# Moshit

A standalone, cross-platform datamoshing tool — no Adobe, no After Effects
plugins, just FFmpeg and Python. Moshit is three things in one package: a
**desktop app** (PySide6 GUI), a **command-line tool**, and an **embeddable
engine** with no third-party Python dependencies.

Datamoshing is the art of corrupting compressed video on purpose — dropping
keyframes and smearing motion so one shot bleeds into the next. Moshit does it by
editing the compressed bytes directly, then bakes the result back into normal,
playable video.

## How it works

Datamoshing is byte surgery on compressed video. Moshit:

1. **Transcodes** any input to a *moshable intermediate*: MPEG-4 Part 2 in an
   AVI container, B-frames disabled. AVI stores every frame as a discrete chunk
   and tolerates a stream that doesn't start on a keyframe — exactly what a mosh
   produces.
2. **Moshes** by manipulating those frame chunks directly — keeping or dropping
   keyframes (I-frames) and appending, repeating, reordering or substituting the
   inter frames (P-frames) that carry motion. This is pure Python, with no
   re-encoding.
3. **Transcodes out** to a delivery format, baking the corruption in as real
   pixels.

Frames are classified by reading the MPEG-4 VOP coding type straight from the
bitstream, with the AVI index used only as a cross-check. The heavy lifting
(decode/encode) is FFmpeg's; the moshing itself is plain Python.

## Requirements

- Python 3.9+
- FFmpeg with the `mpeg4` encoder (effectively every normal build); `ffmpeg` and
  `ffprobe` must be on your `PATH`
- PySide6 — only if you want the GUI (`pip install 'moshit[gui]'`)
- numpy — for the **raw-frame effects** (pixel sort, RGB recurse/shift, the CDP
  audio-bend family) (`pip install 'moshit[raw]'`)
- OpenCV (plus numpy) — additionally for **optical-flow motion transfer** and
  `motion_magnify` (`pip install 'moshit[flow]'`); flow runs on the GPU via
  OpenCV's OpenCL backend (including AMD through Mesa rusticl), CPU otherwise.
  `[flow]` includes numpy, so it also enables the raw effects.

The core engine and CLI themselves have **no** third-party Python dependencies;
the GUI, raw effects and optical flow are opt-in extras.

On Arch:

```sh
sudo pacman -S ffmpeg
```

Check what your FFmpeg build can do (and whether optical flow is available and
GPU-backed):

```sh
python -m moshit.cli probe
```

## Quick start

```sh
pip install PySide6          # one-time, in addition to ffmpeg
python run_gui.py            # from the project root (the folder with this README)
```

`python run_gui.py` works from any directory. The module form
`python -m moshit.gui` only works from the project root — the folder holding both
this README and the inner `moshit/` package (the one with `__init__.py`). If you
see `No module named 'moshit'`, you're one level too deep; `cd` up. After
`pip install -e .[gui]` you also get a `moshit-gui` command that works anywhere.

Typical flow: **Import video** (one import; the clip is then available to either
track) → select it in the library and **Add to main** or **Add to motion** → click
the base clip → pick an effect (e.g. `motion_splice`), choose a motion source, and
**+ Add** it to the clip's effect stack → scrub or play the result → stack more
effects, or **Bake stack** to freeze it (reversible) → **Export…**. A clip can sit
on both tracks at once, and any imported clip can be used as a motion source.

## The app

The window is a media library, a preview with transport controls, a multi-track
timeline (stacked video tracks plus a motion-source track, with a sequence
switcher above it), and an effect inspector whose controls are generated
automatically from each effect's parameter schema — so any effect, including ones
you write yourself, gets a usable UI with no GUI code.

**Timeline.** A scrub handle rides the ruler across the top; drag it to move
through the preview (it also tracks playback). Two tools sit in a strip directly
under the timeline:

- **Pointer** — drag a clip's body to move it in time (free positioning; gaps
  and overlaps allowed), or drag either edge to trim its start or end. Dragging
  **snaps** to nearby clip edges, the playhead and the start (hold **Alt** to
  place freely), and a ghost shows exactly where the clip will land.
- **Cut** — click a clip to split it at that frame into two clips.

Bring footage in by **dropping video files** anywhere on the window (they import
as one batch), and **drag a clip from the media library straight onto a track**
to place it at that point — with the same snapping, so clips butt cleanly.
Clips carrying mosh effects show an **≋N** badge, and a melting Easy-mode cut
shows an orange notch on its left edge. **Multi-select** with **Ctrl-click**
(toggle) or **Shift-click** (range on a track); the primary clip (the one the
inspector edits) gets the brightest outline, and dragging any member moves the
whole selection together. **Copy** (`Ctrl+C`) the selection — each clip *with
its effect stack* — and **Paste** (`Ctrl+V`) it at the playhead, preserving the
clips' spacing. You can also **split at the playhead** (`S`, or
Edit → Split at playhead) and **duplicate** a clip with its effect (`Ctrl+D`, or
right-click → Duplicate); right-click a clip for those plus Copy/Paste and
Remove. Delete (or right-click → Remove) takes the selected clips off the
timeline in one step. **Undo** and **Redo** (Ctrl+Z / Ctrl+Shift+Z, under the
Edit menu) cover timeline and effect edits — add, move, trim, cut, duplicate,
remove, and effect changes — and each menu entry names the step it will reverse
("Undo Trim clip"). Baking and the optical-flow transfer are undoable too, and a
bake is separately reversible with **Revert bake**. Undo is unavailable while a
bake, flow or export is running, since those commit their own step on finishing.
Edits re-render the
preview automatically after a short pause; toggle **Auto-refresh** off in the
toolbar to render only on demand with **Refresh preview** (useful on large
projects). A **waveform strip** under the ruler shows the assembled audio, so you
can line edits up to the sound at a glance.

**Easy mode.** The classic datamosh cut — one shot's pixels carried along by the
next shot's motion — with zero setup: toggle **Easy mode** in the toolbar and
just add clips end to end. Every clip added after another one gets the keyframe
at its cut deleted automatically, so each cut melts into the next clip, and any
number of clips chain into one continuous smear sequence. The transition is an
ordinary `iframe_removal` op on the new clip (region limited to the cut), so it
shows in the inspector's effect stack where it can be tweaked, disabled or
removed per clip — and every other editing tool keeps working as usual. The
first clip on a track is left intact so the sequence still opens on a clean
image; the melt re-blooms at the next natural keyframe unless you widen the
effect's region.

**Preview.** Frames are decoded with FFmpeg, so the GUI needs nothing beyond
PySide6 — no extra media libraries. The preview streams in as it decodes (you see
it build rather than waiting on a frozen window), and your scrub position is kept
across re-renders so iterating on an effect doesn't jump you back to the start.
The transport has play/pause (`Space`), single-frame step (`,` / `.`),
jump-to-start/end (`Home` / `End`), a **Loop** toggle, a timecode readout
(`frame / frame · mm:ss:ff`), and a **🔊** toggle that plays the assembled
**audio** in sync with playback (built lazily and cached, so most edits don't
rebuild it; needs PySide6's QtMultimedia, otherwise the toggle is hidden).
Press **I** / **O** to mark a **loop sub-range** at the playhead (looping then
repeats just that span; **Shift+I** clears it), and it shows as a band on the
scrub bar. Hold **Source** to flash the clean, un-moshed frame under the
playhead for an **A/B** comparison. The frame **fits** the pane by default;
**Ctrl+wheel** zooms (**1:1** and **Fit** buttons reset it) and you drag to pan
when zoomed in. **File → Save frame as image…** (`Ctrl+Shift+F`) writes the
current frame as a full-resolution PNG.

**Sequence settings.** Every clip is normalised to one resolution and frame rate
on import, so you pick them up front: **New project** (`Ctrl+N`) opens a settings
dialog with presets (720p, 1080p, vertical, …) or a custom size, and **File →
Project settings…** changes them while the project is still empty. They lock once
media is imported.

**Clip finishing (speed · reverse · fades · crossfade).** Selecting a main clip
shows clip controls above the effect inspector: **Speed** (e.g. 2× / 0.5×),
**Reverse**, **Fade in** / **Fade out** (to/from black, in frames), and
**Crossfade ⟵** (dissolve in from the previous clip over N frames). These are
clean, pixel-domain edits — distinct from the codec-domain mosh effects — so they
compose with moshing: you can mosh a clip *and* slow it, reverse it, or dissolve
into it. They only kick in a re-encode when used; plain moshing keeps the fast
codec-only path. Speed shows on the timeline as a `2×` badge (reverse `⇄`, fades
`⊳`/`⊲`), and a crossfade shows as a hatched overlap band where the two clips
dissolve — the same region the renderer blends.

**Effect stacks.** A clip holds a *stack* of effects, applied top to bottom — so
glitches compound (e.g. `pframe_duplicate` → `bitrot` → `pframe_shuffle`). The
inspector lists the stack: **+ Add** appends the effect configured below, **↑/↓**
reorder it (order changes the look), the per-row checkbox enables/disables an
effect without removing it, **🎲** re-rolls the selected effect's parameters
(within each parameter's range) for quick happy accidents, and **− Remove**
deletes it. Select a row to edit that effect's mode and parameters. Saved
**presets** can be applied to a clip either way — **Apply ▾** offers *Replace
stack* or *Append to stack*. **Bake stack** freezes the whole chain into one clip
(reversible), leaving the clip's finishing editable.

**Parameter automation.** Numeric parameters that an effect marks *automatable*
get an **A** toggle in the inspector. Switch it on and **Curve…** opens a keyframe
editor: add any number of points (each a normalised position 0–1 and a value),
choose an easing — **linear**, **smooth** (eased in/out), or **hold** (stepped) —
and watch the curve preview. So a glitch can build and release (e.g. `bitrot`
corruption rising 0 → 0.9 then back, or a `pframe_duplicate` bloom that peaks mid
clip), not just ramp once. The curve is keyed to position over the frames the
effect processes, so it survives effects that change the frame count. Built-in
automatable params include `bitrot` intensity, `pframe_duplicate` factor, and
`surge` intensity; effects opt in (see *Writing an effect*).

The **♪** button next to an automatable param fills it straight from the music:
it detects the beats in the preview audio that fall under the selected clip and
writes a stepped (hold) curve that spikes the value on each one — so a bloom or
bitrot burst lands on the beat. Unmute and render a preview first so there's
audio to analyse; the detection is pure-Python (no extra dependencies).

**Region.** Each effect can be limited to a frame range of its input — tick
**Limit to frames** and set a start/end (leave end at `end` to run to the last
frame). So you can glitch just the middle of a clip, or stack effects that hit
different parts. Automation ramps over the region when both are set, and the
range is keyed to the effect's input, so for the first effect it's the clip.

**Presets & randomiser.** **Save…** stores the selected clip's whole stack
(modes, params, automation, regions, enabled flags) as a named preset; pick one
from the dropdown and **Apply** to drop it onto another clip (presets live in
`~/.config/moshit/presets.json`, shared across projects). The **🎲** button rolls
random values into the parameters below — a fast way to find a look, which you
then **Add** or **Apply**.

**Pixel FX.** A second, pixel-domain class of effect (distinct from the
codec-domain mosh stack): clean FFmpeg-filter looks applied in the finish pass,
*after* the mosh and the speed/fade finishing. The inspector's **Pixel FX** panel
adds, edits and removes them per clip. Built-ins: `rgb_shift` (chromatic
aberration), `hue_rotate`, `pixelate`, `noise`, `echo` (frame ghosting), and
`trails` (light streaks). They compose with everything — mosh a clip, slow it,
crossfade it, *and* shift its channels — and bake/persist like the other clip
finishing. `moshit modes` lists them under their own heading.

**Motion injection.** Synthetic camera moves, also pixel-domain, that *animate
across the exact clip length*: `zoom` (push in / pull out, start× → end×), `pan`
(drift by a pixel offset with headroom so edges stay filled), `rotate` (static
angle plus optional total spin), and `shake` (hand-held jitter). They're Pixel FX
like any other (added from the same panel, composable, bake-able); because they
know the clip's geometry and frame count, a `zoom` of 1→2 ramps evenly over the
whole clip. For motion driven by *another* clip's movement rather than a fixed
move, use optical-flow transfer (below).

**Raw FX (pixel sorting).** A third effect class — *raw-frame* processors that
work on decoded pixels in numpy, for looks FFmpeg filters can't express. The
first is **`pixel_sort`**: it sorts the pixels of each row (or column) within a
brightness threshold band, leaving out-of-band pixels anchored, so bright (or
dark) regions smear into the classic sorted-glitch streaks. Controls: `axis`
(horizontal/vertical), `by` (brightness/hue/saturation), `lo`/`hi` (the band),
and `order` (ascending/descending). It lives in its own inspector **Raw FX**
panel, runs *before* the FFmpeg pixel filters in the finish pass, and — like
optical-flow transfer — needs numpy (the `flow` extra); without it the effect is
skipped rather than failing. The sort is fully vectorised (a single `lexsort`
per frame), so it's cheap despite working per-pixel. A second raw effect,
**`rgb_recurse`**, rolls the colour channels apart and permutes them and feeds
the result back into itself over `iterations` passes (cross-faded by `decay`),
so chromatic fringing compounds into deep, recursive colour trails. A third,
**`rgb_iterative_shift`**, is a faithful numpy port of the Processing
"ChannelShiftGlitch": each of `iterations` passes copies one random RGB channel
into another at a random wrapped `shift_horizontal`/`shift_vertical` offset,
tearing the planes into drifting ghost registrations — `recursive` feeds each
pass back in so shifts compound, `seed` makes it repeatable, and `animate`
re-rolls the pattern per frame for a shimmering version.

**Codec motion & stutter.** Two codec-domain mosh modes (the same class as
`pframe_duplicate`) give finer control over coded motion. **`motion_gain`** is a
single continuous knob: above 1.0 it re-applies P-frame motion for exaggerated,
blooming movement; below 1.0 it thins P-frames out toward a freeze at 0
(fractional gains are error-diffused, so 1.5 doubles every other P-frame). Its
pixel-domain twin, the raw effect **`motion_magnify`**, instead measures real
motion with optical flow and re-warps each frame by `factor` × its displacement
— exaggerating movement (the "motion microscope") above 1, damping it below 1,
and stabilising content toward the opening frame at 0 (needs the `flow` extra).
**`pframe_stutter`** chops each P-frame run into blocks of a chosen `length` and
replays every block `repeats` times — `forward` for a hard stutter, `reverse` so
each echo replays the deltas backwards, or `pingpong` for out-and-back.

**RAW DATA - AUDIO (databending through CDP).** A family of raw effects that run
a clip's pixels through **audio** software for vivid, unpredictable corruption.
Every byte of every frame becomes one mono 16-bit sample (the whole clip
concatenated into one stream, so the effect smears colour and frames into each
other), the stream is processed by a [Composer Desktop
Project](https://www.composersdesktop.com/) (CDP) sound-transformation program,
and the result is mapped back to pixels and **length-fitted** to the clip's exact
geometry — so a render's frame count and size never change, and a CDP failure is
a clean no-op. The first set wraps CDP's **`distort`** waveset family, each with
*all* its parameters exposed: `cdp_distort_multiply`/`divide` (waveset
frequency), `cdp_distort_repeat`/`interpolate` (stutter / smear groups of
wavesets), `cdp_distort_telescope` (collapse runs into one), `cdp_distort_reverse`
(granular backwards), and `cdp_distort_omit` (rhythmic silence dropouts). They
live in the **Raw FX** panel under a *RAW DATA - AUDIO* group, alongside the other
raw effects. CDP is **not bundled** — it's a large third-party toolkit you
install yourself (free from composersdesktop.com). Moshit discovers its binaries
at runtime from `$MOSHIT_CDP_DIR`, or failing that a `CDP8/NewRelease` folder
beside the repo (git-ignored, so you drop your own copy there); without them the
whole group simply doesn't appear, and any individual CDP failure is a clean
no-op. The prebuilt binaries are Linux; on other platforms, point
`$MOSHIT_CDP_DIR` at a native CDP install or the group stays hidden. Adding a CDP
program is one descriptor entry — its controls are generated from the parameter
schema, no GUI changes.

**Masks (mattes).** Any clip can carry two mattes, both keyed by **luminance**,
**alpha**, **motion** (frame-to-frame difference) or **chroma** (a green-screen
key on a chosen `key` colour), with a soft `lo`/`hi` threshold band, `invert`,
and `feather`. A **layer matte** modulates the clip's compositing alpha, so it
shows through only where the matte is bright (a luma matte keys out darks; a
motion matte reveals only moving areas; a chroma matte drops the key colour —
the track below fills the rest). An **FX matte** instead gates the clip's
effects — both the FFmpeg pixel FX *and* the numpy raw FX (e.g. pixel sort) —
and has a **mode**: `confine` keeps the effect strictly inside the matte (no
overspill), while `source` *generates* the effect from the matte-cut island and
lets its output spill outward over the rest of the frame (glitches that
originate in a region but bleed past it). Both mattes live in the inspector's
**Masks** panel; a layer matte on a lone track composites it over black.

The **alpha** source uses real **source-file transparency**: importing an
alpha-carrying file (PNG / ProRes 4444 / WebM / MOV …) captures a grayscale
alpha map alongside the moshable intermediate, so an alpha matte composites the
overlay correctly — transparent areas reveal the track below. The map is encoded
with the same GOP structure as the picture, so it can be **datamoshed right
alongside it**: a codec-moshed clip runs its alpha map through the identical op
chain, and the transparency blooms and smears *in sympathy with* the glitch
while staying frame-aligned. Only the finish-stage re-timings — **speed,
reverse, optical-flow** — re-time the picture out from under the map, so those
fall back to opaque (use a chroma key there instead).

**Optical-flow transfer (appearance-free motion transfer).** Warp a clip's pixels
by the *motion* of another clip — dense optical flow drives the warp, so only the
base's pixels are resampled and **none of the driver's appearance bleeds in** (the
clean counterpart to `motion_splice`). Two ways to use it:

- **Flow FX** (live) — the inspector's Flow FX panel: pick a motion source, a
  strength, hold-vs-follow / accumulate, and an optional frame range. It's a live,
  region-scoped clip effect that re-renders as you tweak and composes with the
  mosh stack, speed/fade and pixel FX (length-preserving).
- **Optical-flow transfer…** (bake) — the button renders a new, reversible clip
  onto the timeline (a flat result, no recompute).

This is the one GPU-capable corner: with the `flow` extra installed, OpenCV's
OpenCL backend runs the flow and the warp on the GPU (AMD via Mesa rusticl,
Intel, or NVIDIA), falling back to CPU. From the CLI:

```sh
python -m moshit.cli flow --base footage.mp4 --motion fire.mp4 \
    --strength 1.5 --out warped.avi --export h264_mp4 --export-out warped.mp4
```

**Generated motion (transforms).** The **Generate** menu makes procedural motion
sources — zoom in/out, horizontal/vertical pan, and rotate — and drops them on
the motion track. Each is a static, detailed texture moved by the chosen
transform, so it carries that geometry as codec motion vectors: pick one as a
`motion_splice` (or `motion_weave`) source and the zoom/pan/rotate is transferred
onto your base clip. The source's texture bleeds through along with the motion —
that is the motion-transfer look. (For *appearance-free* transfer — motion with
no source texture bleeding through — use **Optical-flow transfer** above.)

**Where files go.** Imported clips are transcoded to a moshable intermediate, and
baked clips and preview renders are written to a per-session temporary folder.
That folder is deleted when the app closes (and via an exit hook if the process
is interrupted). Nothing persists unless you **File → Save**, which copies the
project's media into a `<name>_assets` folder beside the saved `.json`. Source
files are never touched. Closing the window — or **New** / **Open** — with
unsaved changes prompts you to save first, and the title shows a `*` while there
are unsaved edits.

## Command line

The CLI exposes the same engine for scripting and batch work.

### Motion transfer (the signature effect)

Hold a base clip's first frame and drive it with another clip's motion:

```sh
python -m moshit.cli mosh \
    --base footage.mp4 --motion fire.mp4 --mode motion_splice \
    --width 1280 --height 720 --fps 30 \
    --out moshed.avi --export h264_mp4 --export-out moshed.mp4
```

### Other built-in effects

```sh
# Classic smear: drop interior keyframes (use a smaller GOP so they exist)
python -m moshit.cli mosh --base clip.mp4 --mode iframe_removal \
    --param keep_first=true --gop 15 --out smear.avi

# Bloom / pulse: repeat P-frames to stretch motion
python -m moshit.cli mosh --base clip.mp4 --mode pframe_duplicate \
    --param factor=3 --param stride=2 --out bloom.avi

# Chaotic jitter: shuffle P-frame order
python -m moshit.cli mosh --base clip.mp4 --mode pframe_shuffle \
    --param seed=7 --out shuffle.avi

# Blocky corruption: scramble bytes inside P-frames (the decoder reports
# "damaged" macroblocks — that is the glitch, and the clip still decodes)
python -m moshit.cli mosh --base clip.mp4 --mode bitrot \
    --param intensity=0.4 --param hits=10 --out bitrot.avi

# Braid two motions: interleave base and motion-source P-frames
python -m moshit.cli mosh --base clip.mp4 --motion fire.mp4 --mode motion_weave \
    --param base_run=1 --param motion_run=2 --out weave.avi
```

### Effects

Each effect lives in its own file under `moshit/modes/`:

| effect | what it does |
|--------|--------------|
| `motion_splice`    | hold the base frame and apply a motion source's P-frames (motion transfer) |
| `motion_weave`     | interleave base and motion-source P-frames (braid two motions) |
| `iframe_removal`   | drop keyframes so motion smears across cuts |
| `pframe_duplicate` | repeat P-frames into a bloom/pulse |
| `pframe_shuffle`   | shuffle P-frame order for chaotic jitter |
| `pframe_reverse`   | replay P-frame deltas inverted |
| `pframe_drop`      | randomly drop P-frames for stutter/skip |
| `bitrot`           | corrupt bytes inside P-frames for blocky artefacts |
| `gop_scramble`     | shuffle whole keyframe-anchored GOP blocks for jump-cuts |
| `pingpong`         | replay P-frame runs forward then reversed (boomerang) |
| `pframe_echo`      | re-apply P-frames as delayed echoes (motion trails) |
| `pframe_stutter`   | chop P-frame runs into blocks and replay each N times (stutter) |
| `momentum`         | retime the P-frames to ease the motion in or out |
| `motion_gain`      | one knob: >1 re-applies P-frame motion (bloom), <1 thins toward a freeze |
| `iframe_pulse`     | re-inject the keyframe on a beat for a strobing pulse |
| `surge`            | randomly repeat P-frames in clusters for lurching surges |

(The pixel-domain **Pixel FX** and numpy **Raw FX** are separate classes,
described above and listed under their own headings by `moshit modes`.)

List every effect and parameter at any time:

```sh
python -m moshit.cli modes
```

### Export profiles

`h264_mp4`, `h265_mp4`, `prores_mov` (422 HQ), `ffv1_mkv` (lossless),
`vp9_webm`. Pass `--hwaccel vaapi` to use the GPU for H.264/H.265 export. The
mosh and the intermediate are always CPU — only MPEG-4 Part 2 is moshable, and no
GPU produces it.

Export reassembles audio from the original source clips and muxes it in (the GUI
export dialog has an **Include audio** toggle; `render-project` does it by
default, disable with `--no-audio`). Each video track is reassembled to the
rendered video's exact length and the tracks are summed (per-clip **gain**), so
trims, cuts and clip moves stay in sync across the whole composition.

### Non-destructive projects

Editing is non-destructive: source files are never modified, and **baking** a
mosh is reversible. A bake renders the result into a new clip and swaps it onto
the timeline, but the original clips and the mosh recipe stay in the project file
— archived, not deleted — so you can revert.

Walk the whole lifecycle (import → render → save → bake → revert) end to end:

```sh
python -m moshit.cli demo-project \
    --base footage.mp4 --motion fire.mp4 --out-dir myproject
```

Render a saved project:

```sh
python -m moshit.cli render-project myproject/project.json --out final.avi
```

### Compositing tracks & nested sequences

A sequence holds one or more **video tracks**. Clips are positioned freely on a
track (gaps allowed); a single contiguous track at full opacity and the `normal`
blend takes the codec-domain fast path, while anything else — a second track, a
gap, a per-clip **opacity**, a **blend mode** (`screen`, `multiply`, `add`,
`difference`, …), or two clips that **overlap on the same track** — composites
bottom-to-top in the pixel domain. It's alpha-aware, so gaps show the track below
and overlapping same-track clips **cross-dissolve**.

A whole sequence can be used as a clip inside another — an After-Effects-style
**precomp**. A precomp renders to a cached intermediate (re-rendered only when its
contents change, and guarded against reference cycles) and behaves like any other
media, so you can **mosh, retime, and composite a precomp** just like a source
clip. Audio still comes from the root sequence's main track.

In the app, the **timeline shows the current sequence's video tracks** stacked
top-to-bottom (the top lane composites over the ones below), with the motion pool
at the bottom; clips show opacity/blend badges. Right-click an empty lane to add /
remove / move / enable a track or drop the selected media onto it; set a clip's
**Opacity** and **Blend** in the inspector. The **Sequence** bar above the
timeline switches sequences and **Precompose**s the selected clip into a new one;
**double-click a precomp clip** to step into it. The preview renders whichever
sequence you're editing.

### Tests

A dependency-light check (no FFmpeg needed) of the AVI codec, the effects, the
finishing/automation/region math, presets, and the non-destructive bake/revert
bookkeeping:

```sh
python -m moshit.cli selftest
```

The `pytest` suite adds **ffmpeg-gated integration tests** — real render/export,
audio sync, speed/reverse/fade/crossfade, pixel effects, automation and region —
plus a few GUI smoke tests. Integration tests skip automatically when ffmpeg (or,
for the GUI, a usable Qt platform) is missing:

```sh
pip install -e ".[gui,test]"   # or just ".[test]" to skip the GUI tests
pytest
```

CI (GitHub Actions) runs both on every push and PR across Python 3.11–3.12.

## Architecture

```
moshit/
  avi.py        RIFF/AVI reader + writer, VOP frame classification  (stdlib only)
  ffmpeg.py     FFmpeg/FFprobe wrapper, capability probe, transcodes (stdlib only)
  modes/        the effect plugin system
    base.py        MoshMode, Param schema, MoshContext, registry
    loader.py      built-in + user-plugin discovery
    motion_splice.py, iframe_removal.py, …   (one file per effect)
  engine.py     MoshEngine: normalize -> mosh -> write -> bake/export
  project.py    non-destructive project model + JSON persistence
  cli.py        the command-line tool
  gui/          PySide6 desktop app (controller, widgets, ffmpeg preview)
run_gui.py      GUI launcher
```

The engine is completely independent of the GUI and has no third-party Python
dependencies. The `Param` schema is what lets the GUI build controls for any
effect — including third-party ones — without changing any GUI code.

## Writing an effect

Drop a `.py` file into `~/.config/moshit/modes/` (override the location with
`$XDG_CONFIG_HOME`). An effect is a pure function over a list of frames:

```python
from moshit.modes import MoshMode, Param

class EveryOther(MoshMode):
    name = "every_other"
    description = "Drop every other P-frame."
    params = [Param("offset", "int", 0, lo=0, hi=1, label="Offset")]

    def apply(self, frames, ctx, *, offset=0):
        out, p = [], 0
        for f in frames:
            if f.is_pframe:
                if (p + offset) % 2 == 0:
                    out.append(f)
                p += 1
            else:
                out.append(f)
        return out
```

It will appear in `moshit modes`, in the GUI inspector, and via
`--mode every_other`. (A mode file is ordinary Python and runs on load — treat
third-party modes like any script you install.)

To let a numeric parameter be **automated** (ramped over the clip), mark it
`Param(..., automatable=True)` and read its per-frame value inside the loop with
`ctx.auto(name, i, default)`, passing the input-frame index `i`. When the param
isn't automated, `ctx.auto` just returns `default` (your static value), so the
same code path serves both:

```python
params = [Param("amount", "float", 0.5, lo=0.0, hi=1.0, automatable=True)]

def apply(self, frames, ctx, *, amount=0.5):
    out = []
    for i, f in enumerate(frames):
        a = ctx.auto("amount", i, amount)   # ramped if automated, else `amount`
        ...
    return out
```

## Known limits (v1)

- The renderer composites stacked **video tracks** (opacity + blend mode + alpha)
  and supports **nested sequences** (precomps), both editable from the timeline.
  Clips are **freely positioned** (drag a clip's body to move it; gaps show black /
  the track below), and clips that **overlap on the same track cross-dissolve**.
  Editing ops are non-rippling (a delete or trim leaves a gap rather than closing
  it). Audio is **mixed across all video tracks**: each track is laid out at its
  clips' positions with gap silence, then the tracks are summed (per-clip **gain**
  in the inspector controls levels).
- Clip trims snap to the nearest preceding keyframe so every clip stays
  decodable (GOP-based editing; for frame-exact cuts, use a smaller GOP).
- Audio is reassembled from the original sources and muxed on **export** (the
  moshable intermediate stays video-only). Clean edits stay perfectly in sync;
  moshed clips keep their source audio padded/trimmed to the retimed length;
  baked clips are silent. The **preview plays this audio** in sync via the **🔊**
  toggle (built lazily and cached; needs PySide6's QtMultimedia), and the same
  track is muxed into the export — which needs the source files still on disk.
- Baking re-encodes (one generation of MPEG-4 recompression) in exchange for a
  clean, predictable, re-moshable clip.

## Roadmap

The basic-editing polish list is cleared. Recently landed: **visual crossfade
overlap** (the timeline draws the true overlap as a hatched band, with a
frame-accurate, overlap-aware ruler), **undo/redo** (snapshot-based history,
Ctrl+Z / Ctrl+Shift+Z), **clip split at playhead** (GOP-snapped so both halves
stay decodable), an **audio waveform strip** under the ruler, and **beat-synced
keyframes** (the ♪ button pulses an automatable parameter on the audio's beats).

**Compositing tracks and nested sequences** have landed end-to-end: multiple
video tracks composited with opacity/blend/alpha and precomps rendered to cached,
moshable media, all editable from the timeline (track management, per-clip
opacity/blend, free clip positioning with gaps, intra-track crossfade dissolves,
a sequence switcher, precompose, and double-click-to-enter). **Multi-track audio
mixing** has now landed too: every video track contributes audio (laid out with
gap silence) and the tracks are summed with per-clip **gain**.

On the glitch side, the signature systems have all landed: GPU optical-flow
motion transfer (see **Optical-flow transfer**), per-clip optical-flow as a
live region-scoped *effect*, multi-keyframe automation curves with per-keyframe
easing, **motion injection** (synthetic zoom/pan/rotate/shake camera moves),
**pixel sorting** (a threshold-banded numpy raw-frame effect, the first of a new
*Raw FX* class), and **masking** — both compositor layer-mattes *and* finish-pass
effect-mattes, keyed by luminance / motion / **chroma** with a soft threshold
band, invert and feather, a **confine/source** mode that controls whether
glitches stay inside the matte or spill out of it, and **source-file alpha**
(real transparency from alpha-carrying imports, via a captured alpha map that
aligns to clean placements). That clears the three glitch families that were on
the bench along with every matte source originally planned; the engine now spans
codec-domain mosh, pixel/raw finishing, motion transfer, compositing with
luma/alpha/motion/chroma mattes, and nested sequences.
