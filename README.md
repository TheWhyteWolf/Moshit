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
- OpenCV + numpy — only for **optical-flow motion transfer**
  (`pip install 'moshit[flow]'`); it runs on the GPU via OpenCV's OpenCL backend
  (including AMD through Mesa rusticl), CPU otherwise

The core engine and CLI themselves have **no** third-party Python dependencies;
the GUI and optical flow are opt-in extras.

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

The window is a media library, a preview with transport controls, a two-track
timeline (a main track and a motion-source track), and an effect inspector whose
controls are generated automatically from each effect's parameter schema — so any
effect, including ones you write yourself, gets a usable UI with no GUI code.

**Timeline.** A scrub handle rides the ruler across the top; drag it to move
through the preview (it also tracks playback). Two tools sit in a strip directly
under the timeline:

- **Pointer** — drag a clip's body to reorder it on the main track, or drag
  either edge to trim its start or end.
- **Cut** — click a clip to split it at that frame into two clips.

You can also **split at the playhead** (`S`, or Edit → Split at playhead) and
**duplicate** a clip with its effect (`Ctrl+D`, or right-click → Duplicate);
right-click a clip for those plus Remove. Delete (or right-click → Remove) takes
a clip off the timeline. **Undo** and **Redo** (Ctrl+Z / Ctrl+Shift+Z, under the
Edit menu) cover timeline and effect edits — add, move, trim, cut, duplicate,
remove, and effect changes. Baking is a commit point: it starts a fresh undo
history, and is separately reversible with **Revert bake**. Edits re-render the
preview automatically after a short pause; toggle **Auto-refresh** off in the
toolbar to render only on demand with **Refresh preview** (useful on large
projects).

**Preview.** Frames are decoded with FFmpeg, so the GUI needs nothing beyond
PySide6 — no extra media libraries. The preview streams in as it decodes (you see
it build rather than waiting on a frozen window), and your scrub position is kept
across re-renders so iterating on an effect doesn't jump you back to the start.
The transport has play/pause (`Space`), single-frame step (`,` / `.`),
jump-to-start/end (`Home` / `End`), a **Loop** toggle, a timecode readout
(`frame / frame · mm:ss:ff`), and a **🔊** toggle that plays the assembled
**audio** in sync with playback (built lazily and cached, so most edits don't
rebuild it; needs PySide6's QtMultimedia, otherwise the toggle is hidden).
**File → Save frame as image…** (`Ctrl+Shift+S`) writes the current frame as a
full-resolution PNG.

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
`⊳`/`⊲`), and a crossfade shows as a corner wedge on the clip it fades into.

**Effect stacks.** A clip holds a *stack* of effects, applied top to bottom — so
glitches compound (e.g. `pframe_duplicate` → `bitrot` → `pframe_shuffle`). The
inspector lists the stack: **+ Add** appends the effect configured below, **↑/↓**
reorder it (order changes the look), the per-row checkbox enables/disables an
effect without removing it, and **− Remove** deletes it. Select a row to edit that
effect's mode and parameters, then **Apply to selected effect**. **Bake stack**
freezes the whole chain into one clip (reversible), leaving the clip's finishing
editable.

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
| `momentum`         | retime the P-frames to ease the motion in or out |
| `iframe_pulse`     | re-inject the keyframe on a beat for a strobing pulse |
| `surge`            | randomly repeat P-frames in clusters for lurching surges |

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
default, disable with `--no-audio`). The audio track is built to the rendered
video's exact length, so trims, cuts and reorders stay in sync.

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

- The main track is a sequence of clips laid out contiguously; a mosh targets one
  clip and may pull motion from a clip on the motion track. A crossfade overlaps
  two clips at render time, but the timeline still draws them edge-to-edge (with a
  marker) — full compositing tracks are future work.
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

Remaining basic-editing polish:

- **Visual crossfade overlap** — crossfades render correctly, but the timeline
  still lays clips out contiguously (with a corner marker) rather than drawing
  the true overlap; a compositing track is the longer-term home for that.

On the glitch side, the signature systems have all landed: GPU optical-flow
motion transfer (see **Optical-flow transfer**), per-clip optical-flow as a
live region-scoped *effect*, and multi-keyframe automation curves with
per-keyframe easing.
