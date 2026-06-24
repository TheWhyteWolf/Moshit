"""Command-line harness for the Moshit engine.

    moshit probe                     show what this ffmpeg build can do
    moshit modes                     list effects and their parameters
    moshit mosh ...                  mosh a base clip with a motion source
    moshit flow ...                  appearance-free optical-flow motion transfer
    moshit demo-project ...          end-to-end non-destructive project demo
    moshit render-project p.json     render a saved project
    moshit selftest                  pure-Python checks (no ffmpeg needed)
"""
from __future__ import annotations

import argparse
import struct
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__
from .avi import Frame, parse_avi, write_avi
from .engine import EngineConfig, MoshEngine, _ext_for_profile
from .ffmpeg import FFmpeg, FFmpegError
from .modes import (
    MoshContext,
    available_modes,
    get_mode,
    load_modes,
    resolve_automation,
)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _engine(args) -> MoshEngine:
    ff = FFmpeg(ffmpeg=getattr(args, "ffmpeg", None),
                ffprobe=getattr(args, "ffprobe", None))
    cfg = EngineConfig(width=args.width, height=args.height, fps=args.fps,
                       gop=args.gop, qscale=args.q,
                       work_dir=getattr(args, "work_dir", None))
    return MoshEngine(cfg, ff)


def _coerce(value: str, kind: str):
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    if kind == "bool":
        return value.strip().lower() in ("1", "true", "yes", "on", "y")
    return value


def _parse_params(pairs: Optional[List[str]], mode_name: str) -> Dict:
    mode = get_mode(mode_name)
    schema = {p.name: p for p in mode.params}
    out: Dict = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--param expects key=value, got '{pair}'")
        key, _, val = pair.partition("=")
        key = key.strip()
        if key not in schema:
            raise SystemExit(f"mode '{mode_name}' has no parameter '{key}'. "
                             f"Known: {sorted(schema)}")
        out[key] = _coerce(val, schema[key].kind)
    return out


def _add_engine_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--gop", type=int, default=250)
    p.add_argument("--q", type=int, default=3, help="mpeg4 qscale (2..5, lower=better)")
    p.add_argument("--work-dir", default=None)


# --------------------------------------------------------------------------- #
# probe / modes
# --------------------------------------------------------------------------- #

def cmd_probe(args) -> int:
    ff = FFmpeg(ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
    print(ff.capabilities().report())
    if "vaapi" in ff.capabilities().hwaccels:
        ok = ff.probe_hwaccel("vaapi")
        print(f"  vaapi runtime test: {'works' if ok else 'not usable here'}")
    from . import flow
    if flow.available():
        print(f"  optical flow (opencv): yes -- {flow.backend()}")
    else:
        print("  optical flow (opencv): MISSING (pip install 'moshit[flow]')")
    return 0


def cmd_modes(args) -> int:
    load_modes()
    for name in available_modes():
        mode = get_mode(name)
        print(f"\n{name}\n  {mode.description}")
        for p in mode.params:
            req = " (required)" if p.default is None else ""
            auto = " (automatable)" if getattr(p, "automatable", False) else ""
            print(f"    - {p.describe()}{req}{auto}")
            if p.help:
                print(f"        {p.help}")

    from .modes import (available_pixel_modes, get_pixel_mode,
                        available_raw_modes, get_raw_mode)
    pixel = available_pixel_modes()
    if pixel:
        print("\n=== pixel effects (clip finishing, FFmpeg filters) ===")
        for name in pixel:
            mode = get_pixel_mode(name)
            print(f"\n{name}\n  {mode.description}")
            for p in mode.params:
                print(f"    - {p.describe()}")
    raw = available_raw_modes()
    if raw:
        print("\n=== raw effects (clip finishing, numpy frame processors) ===")
        for name in raw:
            mode = get_raw_mode(name)
            print(f"\n{name}\n  {mode.description}")
            for p in mode.params:
                print(f"    - {p.describe()}")
    return 0


# --------------------------------------------------------------------------- #
# mosh
# --------------------------------------------------------------------------- #

def cmd_mosh(args) -> int:
    eng = _engine(args)
    load_modes()
    params = _parse_params(args.param, args.mode)

    print(f"normalising base: {args.base}")
    base = eng.normalize_clip(args.base, label="base")
    motion_clips = {}
    if args.motion:
        print(f"normalising motion source: {args.motion}")
        motion_clips["motion"] = eng.normalize_clip(args.motion, label="motion",
                                                    single_keyframe=True)

    mode = get_mode(args.mode)
    if any(p.name == "source" for p in mode.params) and "source" not in params:
        if motion_clips:
            params["source"] = "motion"
        else:
            raise SystemExit(f"mode '{args.mode}' needs a motion source; "
                             f"pass --motion FILE")

    print(f"  base:   {base.summary()}")
    if motion_clips:
        print(f"  motion: {motion_clips['motion'].summary()}")

    frames = eng.mosh(base, args.mode, params, motion_clips=motion_clips)
    out = eng.write_moshed(frames, base, args.out)
    n_i = sum(1 for f in frames if f.is_iframe)
    n_p = sum(1 for f in frames if f.is_pframe)
    print(f"moshed -> {out}  ({len(frames)} frames: {n_i} I, {n_p} P)")

    if args.export:
        ep = Path(args.export_out) if args.export_out else out.with_suffix(
            _ext_for_profile(args.export))
        print(f"exporting [{args.export}] -> {ep}")
        eng.export(out, ep, args.export, hwaccel=args.hwaccel)
        print(f"exported -> {ep}")
    return 0


# --------------------------------------------------------------------------- #
# flow  (appearance-free optical-flow motion transfer; needs opencv)
# --------------------------------------------------------------------------- #

def cmd_flow(args) -> int:
    from . import flow
    if not flow.available():
        raise SystemExit("Optical-flow transfer needs OpenCV + numpy. Install "
                         "with: pip install 'moshit[flow]'")
    eng = _engine(args)
    print(f"flow backend: {flow.backend()}")
    print(f"normalising base:   {args.base}")
    base = eng.normalize_clip(args.base, label="base")
    print(f"normalising motion: {args.motion}")
    motion = eng.normalize_clip(args.motion, label="motion", single_keyframe=True)
    print(f"  base:   {base.summary()}")
    print(f"  motion: {motion.summary()}")
    out = eng.optical_flow_transfer(
        base.source, motion.source, args.out,
        hold=not args.follow, accumulate=not args.no_accumulate,
        strength=args.strength, preset=args.preset)
    print(f"flow-warped -> {out}  ({len(motion.frames)} frames)")
    if args.export:
        ep = Path(args.export_out) if args.export_out else out.with_suffix(
            _ext_for_profile(args.export))
        eng.export(out, ep, args.export, hwaccel=args.hwaccel)
        print(f"exported [{args.export}] -> {ep}")
    return 0


# --------------------------------------------------------------------------- #
# demo-project  (proves the non-destructive model end-to-end)
# --------------------------------------------------------------------------- #

def cmd_demo_project(args) -> int:
    from .project import Project

    eng = _engine(args)
    load_modes()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    assets = out_dir / "assets"

    proj = Project(name="demo", config=eng.config, assets_dir=str(assets))
    print("importing media (sources are never modified) ...")
    main = proj.import_media(eng, args.base, label="base", role="main")
    fire = proj.import_media(eng, args.motion, label="fire", role="motion")
    clip = proj.add_clip(main.id, track="main")
    proj.add_clip(fire.id, track="motion")
    op = proj.add_mosh("motion_splice", {"source": "fire"}, clip.id)
    print(f"  main clip {clip.id} <- media {main.id} ({main.nb_frames} frames)")
    print(f"  motion media {fire.id} '{fire.label}' ({fire.nb_frames} frames)")
    print(f"  mosh op {op.id}: motion_splice(source=fire) -> clip {clip.id}")

    print("\nrendering recipe (pre-bake, read-only) ...")
    r = proj.render(eng, out_dir / "render_pre_bake.avi")
    print(f"  -> {r['moshed_avi']}  ({r['frames']} frames)")
    proj.save(out_dir / "project.json")
    print(f"  saved project.json  (clips={len(proj.clips)}, ops={len(proj.mosh_ops)})")

    print("\nbaking the mosh op ...")
    rec = proj.bake_op(eng, op.id)
    archived_clips = [c.id for c in proj.clips if c.archived]
    print(f"  bake record {rec.id}")
    print(f"  baked media {rec.baked_media_id} -> new clip {rec.baked_clip_id}")
    print(f"  archived (kept, disabled): clips={archived_clips}, "
          f"ops={[o.id for o in proj.mosh_ops if o.archived]}")
    r2 = proj.render(eng, out_dir / "render_post_bake.avi")
    print(f"  re-render from baked clip -> {r2['moshed_avi']} ({r2['frames']} frames)")
    proj.save(out_dir / "project_baked.json")

    print("\nreverting the bake ...")
    proj.revert_bake(rec.id)
    live_clip = proj.clip(clip.id)
    print(f"  original clip {clip.id}: enabled={live_clip.enabled}, "
          f"archived={live_clip.archived}")
    print(f"  original op {op.id}: enabled={proj.op(op.id).enabled}")
    print(f"  baked media present: {rec.baked_media_id in proj.media}")
    print(f"  bake records remaining: {len(proj.bake_records)}")
    proj.save(out_dir / "project_reverted.json")

    print("\nnon-destructive guarantees held: sources untouched, originals "
          "archived on bake, fully restored on revert.")
    print(f"artefacts in: {out_dir}")
    return 0


# --------------------------------------------------------------------------- #
# render-project
# --------------------------------------------------------------------------- #

def cmd_render_project(args) -> int:
    from .project import Project

    proj = Project.load(args.project)
    ff = FFmpeg(ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
    eng = MoshEngine(proj.config, ff)
    load_modes()
    r = proj.render(eng, args.out, profile=args.export, export_path=args.export_out,
                    audio=not args.no_audio)
    print(f"rendered -> {r['moshed_avi']} ({r['frames']} frames, "
          f"{r['clips_rendered']} clips)")
    if "export" in r:
        print(f"exported -> {r['export']}")
        if "audio" in r:
            print("  (source audio muxed for clean clips)")
        elif not args.no_audio:
            print("  (no source audio found to mux)")
    return 0


# --------------------------------------------------------------------------- #
# selftest  (pure Python: no ffmpeg required)
# --------------------------------------------------------------------------- #

def _synth_avi(path: Path, types: str, w: int = 64, h: int = 48) -> None:
    """Write a structurally valid, video-only AVI with fabricated VOP frames.

    Built by hand (not via write_avi) so it independently exercises the parser.
    """
    def chunk(fourcc: bytes, body: bytes) -> bytes:
        out = fourcc + struct.pack("<I", len(body)) + body
        if len(body) & 1:
            out += b"\x00"
        return out

    n = len(types)
    avih = struct.pack("<IIIIIIIIII", 33333, 0, 0, 0x10, n, 0, 1, 0, w, h) + b"\x00" * 16
    strh = (b"vids" + b"FMP4" + struct.pack("<I", 0) + struct.pack("<HH", 0, 0)
            + struct.pack("<IIIIII", 0, 1, 30, 0, n, 0)
            + struct.pack("<iI", -1, 0) + struct.pack("<HHHH", 0, 0, w, h))
    strf = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24,
                       int.from_bytes(b"FMP4", "little"), w * h * 3, 0, 0, 0, 0)
    strl = chunk(b"LIST", b"strl" + chunk(b"strh", strh) + chunk(b"strf", strf))
    hdrl = chunk(b"LIST", b"hdrl" + chunk(b"avih", avih) + strl)

    type_byte = {"I": 0x00, "P": 0x40, "B": 0x80, "S": 0xC0}
    movi_body = bytearray(b"movi")
    idx1 = bytearray()
    for k, t in enumerate(types):
        # vary payload length (some odd) to exercise padding
        filler = bytes((k * 7 + j) & 0xFF for j in range(3 + (k % 4)))
        data = b"\x00\x00\x01\xb6" + bytes([type_byte[t]]) + filler
        offset = len(movi_body)
        movi_body += b"00dc" + struct.pack("<I", len(data)) + data
        if len(data) & 1:
            movi_body += b"\x00"
        flags = 0x10 if t == "I" else 0
        idx1 += b"00dc" + struct.pack("<III", flags, offset, len(data))
    movi = b"LIST" + struct.pack("<I", len(movi_body)) + bytes(movi_body)
    idx1_chunk = b"idx1" + struct.pack("<I", len(idx1)) + bytes(idx1)
    body = hdrl + movi + idx1_chunk
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(body)) + b"AVI " + body)


class _FakeEngine:
    """Stand-in for MoshEngine that needs no ffmpeg (selftest only).

    Reuses the real mode system and the real AVI writer; fakes bake as a copy so
    the project's non-destructive bookkeeping can be tested without transcoding.
    """

    def __init__(self):
        self._dir = Path(tempfile.mkdtemp(prefix="dm_selftest_"))
        self._n = 0

    def _tmp(self, suffix: str) -> Path:
        self._n += 1
        return self._dir / f"_t{self._n}{suffix}"

    def mosh(self, base, mode_name, params, *, motion_clips=None, region=None):
        mode = get_mode(mode_name)
        values = mode.resolve(params)
        clips = {l: c.frames for l, c in (motion_clips or {}).items()}
        body = list(base.frames if region is None
                    else base.frames[region.start:region.stop])
        automation = resolve_automation(values)
        ctx = MoshContext(fps=base.fps, width=base.width, height=base.height,
                          clips=clips, automation=automation, n_frames=len(body))
        if region is None:
            return mode.apply(body, ctx, **values)
        return (list(base.frames[:region.start]) + mode.apply(body, ctx, **values)
                + list(base.frames[region.stop:]))

    def write_moshed(self, frames, template, out_avi):
        write_avi(out_avi, frames, template)
        return Path(out_avi)

    def bake(self, moshed_avi, out_avi):
        import shutil
        shutil.copy2(moshed_avi, out_avi)
        return parse_avi(out_avi)

    def finish_clips(self, seg_avis, meta, dst):
        # No ffmpeg: just concatenate the segments so render() completes. The
        # frame-count math under test is computed in render() before this call,
        # so a plain concat is enough to exercise it.
        frames: List[Frame] = []
        for s in seg_avis:
            frames.extend(parse_avi(s).frames)
        write_avi(dst, frames, parse_avi(seg_avis[0]))
        return Path(dst)


def _check(cond: bool, msg: str, failures: List[str]) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        failures.append(msg)


def cmd_selftest(args) -> int:
    load_modes()
    failures: List[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="dm_selftest_"))

    print("A. AVI parse / classify / write round-trip")
    types = "IPPPPIPPPP"
    src = tmp / "synth.avi"
    _synth_avi(src, types)
    av = parse_avi(src)
    _check(len(av) == len(types), f"frame count == {len(types)}", failures)
    _check("".join(f.coding_type for f in av.frames) == types,
           "VOP coding types classified correctly", failures)
    _check(av.iframe_indices == [0, 5], "I-frame indices == [0, 5]", failures)
    out = tmp / "rewritten.avi"
    write_avi(out, av.frames, av)
    av2 = parse_avi(out)
    _check([f.data for f in av2.frames] == [f.data for f in av.frames],
           "frame payloads survive write->read", failures)
    _check("".join(f.coding_type for f in av2.frames) == types,
           "coding types survive write->read", failures)

    print("\nB. Mode logic")
    ctx = MoshContext(fps=30, width=64, height=48, clips={})
    base_frames = av.frames                       # IPPPPIPPPP  (10 frames, 8 P)
    src_motion = tmp / "synth_motion.avi"
    _synth_avi(src_motion, "I" + "P" * 15)        # longer motion run (15 P)
    motion = parse_avi(src_motion).frames
    ctx_m = MoshContext(fps=30, width=64, height=48, clips={"m": motion})

    spliced = get_mode("motion_splice").apply(
        list(base_frames), ctx_m, source="m", hold_base_iframe=True,
        match_base_length=True, loop_motion=False)
    _check(spliced[0].is_iframe, "motion_splice holds base keyframe first", failures)
    _check(all(f.is_pframe for f in spliced[1:]),
           "motion_splice body is all P-frames", failures)
    _check(len(spliced) == len(base_frames),
           "motion_splice trims long motion to base length", failures)
    _check(spliced[0].source == base_frames[0].source
           and all(f.source == motion[0].source for f in spliced[1:]),
           "motion_splice: base anchor + motion-source body", failures)

    short = get_mode("motion_splice").apply(
        list(base_frames), ctx_m, source="m", match_base_length=True,
        loop_motion=True)
    _check(len(short) == len(base_frames),
           "motion_splice loops short motion to fill when loop_motion=True", failures)

    removed = get_mode("iframe_removal").apply(
        list(base_frames), ctx, keep_first=True, keep_every=0)
    _check(sum(1 for f in removed if f.is_iframe) == 1,
           "iframe_removal keeps only first keyframe", failures)
    removed_all = get_mode("iframe_removal").apply(
        list(base_frames), ctx, keep_first=False, keep_every=0)
    _check(sum(1 for f in removed_all if f.is_iframe) == 0,
           "iframe_removal(keep_first=False) drops all keyframes", failures)

    dup = get_mode("pframe_duplicate").apply(
        list(base_frames), ctx, factor=3, stride=1, start=0)
    base_p = sum(1 for f in base_frames if f.is_pframe)
    _check(sum(1 for f in dup if f.is_pframe) == base_p * 3,
           "pframe_duplicate(factor=3) triples P-frames", failures)
    _check(sum(1 for f in dup if f.is_iframe) ==
           sum(1 for f in base_frames if f.is_iframe),
           "pframe_duplicate leaves I-frames untouched", failures)

    print("\nC. Project non-destructive bake / revert (no ffmpeg)")
    from .project import Clip, MediaItem, Project

    fake = _FakeEngine()
    proj = Project(name="t", assets_dir=str(tmp / "assets"))

    def add_media(label, role, types_):
        mid = f"media_{label}"
        p = tmp / f"{label}.avi"
        _synth_avi(p, types_)
        dest = proj.assets_dir / f"{mid}.avi"
        dest.write_bytes(p.read_bytes())
        clip_av = parse_avi(dest)
        proj.media[mid] = MediaItem(
            id=mid, source_path=str(p), label=label, role=role,
            intermediate_path=str(dest), width=clip_av.width,
            height=clip_av.height, fps=clip_av.fps, nb_frames=len(clip_av.frames))
        proj._parsed[mid] = clip_av
        return mid

    main_id = add_media("base", "main", "IPPPPPPPPP")
    add_media("fire", "motion", "IPPPPPPPPP")
    clip = proj.add_clip(main_id, "main")
    op = proj.add_mosh("motion_splice", {"source": "fire"}, clip.id)

    r = proj.render(fake, tmp / "render_pre.avi")
    _check(r["frames"] == 10, "render produces a frame sequence", failures)

    pre_media = len(proj.media)
    rec = proj.bake_op(fake, op.id)
    _check(proj.clip(clip.id).archived and not proj.clip(clip.id).enabled,
           "bake archives the original clip (not deleted)", failures)
    _check(proj.op(op.id).archived,
           "bake archives the consumed mosh op (recipe retained)", failures)
    _check(rec.baked_clip_id in {c.id for c in proj.clips},
           "bake adds a baked clip", failures)
    _check(len(proj.media) == pre_media + 1, "bake adds one baked media", failures)
    _check(len(proj.main_clips()) == 1,
           "timeline still has exactly one active main clip after bake", failures)

    # JSON round-trip of the baked state
    saved = proj.save(tmp / "p.json")
    reloaded = Project.load(saved)
    _check(len(reloaded.clips) == len(proj.clips)
           and len(reloaded.bake_records) == len(proj.bake_records),
           "project JSON save/load round-trips", failures)

    proj.revert_bake(rec.id)
    _check(proj.clip(clip.id).enabled and not proj.clip(clip.id).archived,
           "revert re-enables the original clip", failures)
    _check(proj.op(op.id).enabled, "revert re-enables the mosh op", failures)
    _check(rec.baked_media_id not in proj.media,
           "revert removes the baked media", failures)
    _check(rec.baked_clip_id not in {c.id for c in proj.clips},
           "revert removes the baked clip", failures)
    _check(len(proj.bake_records) == 0, "revert clears the bake record", failures)
    _check(len(proj.main_clips()) == 1,
           "timeline back to original single clip after revert", failures)

    print("\nD. Clip editing (duplicate / split)")
    edit_main = add_media("editbase", "main", "IPPPPPPPPP")     # 10 frames
    ec = proj.add_clip(edit_main, "main")
    proj.add_mosh("pframe_duplicate", {"factor": 2}, ec.id)
    before_clips, before_ops = len(proj.clips), len(proj.mosh_ops)
    dup = proj.duplicate_clip(ec.id)
    _check(dup is not None and len(proj.clips) == before_clips + 1,
           "duplicate_clip adds one clip", failures)
    _check(dup.media_id == ec.media_id and dup.in_point == ec.in_point
           and dup.out_point == ec.out_point,
           "duplicate shares the source media and trim", failures)
    _check(len(proj.mosh_ops) == before_ops + 1
           and any(o.target_clip_id == dup.id for o in proj.mosh_ops),
           "duplicate copies the clip's effect onto the copy", failures)

    second = proj.split_clip(dup.id, 4)                          # 4-frame head + 6
    _check(second is not None and proj._clip_length(dup) == 4,
           "split leaves the original as a 4-frame head", failures)
    _check(second is not None and proj._clip_length(second) == 6,
           "split second half holds the 6-frame remainder", failures)
    _check(proj.split_clip(dup.id, 0) is None,
           "split at offset 0 is rejected", failures)

    print("\nE. Audio passthrough plan")
    aproj = Project(name="a", assets_dir=str(tmp / "assets_a"))

    def add_amedia(label, types_, derived=False):
        mid = f"amedia_{label}"
        p = tmp / f"a_{label}.avi"
        _synth_avi(p, types_)
        dest = aproj.assets_dir / f"{mid}.avi"
        dest.write_bytes(p.read_bytes())
        cav = parse_avi(dest)
        aproj.media[mid] = MediaItem(
            id=mid, source_path=str(p), label=label, role="main",
            intermediate_path=str(dest), width=cav.width, height=cav.height,
            fps=cav.fps, nb_frames=len(cav.frames), derived=derived)
        aproj._parsed[mid] = cav
        return mid

    aproj.add_clip(add_amedia("clean", "IPPPPPPPPP"), "main")           # 10 frames
    mclip = aproj.add_clip(add_amedia("moshy", "IPPPPPPPPP"), "main")
    aproj.add_mosh("pframe_duplicate", {"factor": 2}, mclip.id)         # 10 -> 19
    aproj.add_clip(add_amedia("baked", "IPPPP", derived=True), "main")  # 5 frames
    ar = aproj.render(fake, tmp / "a_render.avi")
    plan = ar.get("audio_plan", [])
    fps = aproj.config.fps
    _check(len(plan) == 3, "audio plan has one segment per main clip", failures)
    _check(bool(plan) and plan[0]["silent"] is False
           and abs(plan[0]["duration"] - 10 / fps) < 1e-9,
           "clean clip keeps audio, duration matches its frames", failures)
    _check(len(plan) > 1 and plan[1]["silent"] is False
           and abs(plan[1]["duration"] - 19 / fps) < 1e-9,
           "moshed clip keeps audio at its retimed (19-frame) length", failures)
    _check(len(plan) > 2 and plan[2]["silent"] is True,
           "baked clip is silent (source audio can't map to it)", failures)
    _check(abs(sum(s["duration"] for s in plan) - ar["frames"] / fps) < 1e-9,
           "audio plan total duration matches the rendered video", failures)

    print("\nF. Clean-edit finishing (speed / crossfade) length math")
    fproj = Project(name="f", assets_dir=str(tmp / "assets_f"))

    def add_fmedia(label, types_):
        mid = f"fmedia_{label}"
        p = tmp / f"f_{label}.avi"
        _synth_avi(p, types_)
        dest = fproj.assets_dir / f"{mid}.avi"
        dest.write_bytes(p.read_bytes())
        cav = parse_avi(dest)
        fproj.media[mid] = MediaItem(
            id=mid, source_path=str(p), label=label, role="main",
            intermediate_path=str(dest), width=cav.width, height=cav.height,
            fps=cav.fps, nb_frames=len(cav.frames))
        fproj._parsed[mid] = cav
        return mid

    c0 = fproj.add_clip(add_fmedia("a", "I" + "P" * 11), "main")   # 12 frames
    c0.speed = 2.0
    c1 = fproj.add_clip(add_fmedia("b", "I" + "P" * 9), "main")    # 10 frames
    c1.transition_in = 4
    _check(fproj._clip_length(c0) == 6,
           "2x speed: 12 source frames -> 6 timeline frames", failures)
    _check(fproj._clip_length(c1) == 10, "no speed: length unchanged", failures)
    _check(c0.has_finish() and not Clip(id="z", media_id="m", track="main").has_finish(),
           "has_finish() detects finishing vs. a plain clip", failures)
    fr = fproj.render(fake, tmp / "f.avi")
    _check(fr["frames"] == 12,
           "crossfade overlap subtracts from the total (6 + 10 - 4 = 12)", failures)
    fplan = fr["audio_plan"]
    ffps = fproj.config.fps
    _check(fplan[0]["speed"] == 2.0
           and abs(fplan[0]["duration"] - 6 / ffps) < 1e-9,
           "audio plan carries speed and the finished duration", failures)
    _check(fplan[1]["transition_in"] == 4,
           "audio plan carries the crossfade length", failures)

    print("\nG. Effect stacking (multiple ops per clip)")
    gproj = Project(name="g", assets_dir=str(tmp / "assets_g"))

    def add_gmedia(label, types_):
        mid = f"gmedia_{label}"
        p = tmp / f"g_{label}.avi"
        _synth_avi(p, types_)
        dest = gproj.assets_dir / f"{mid}.avi"
        dest.write_bytes(p.read_bytes())
        cav = parse_avi(dest)
        gproj.media[mid] = MediaItem(
            id=mid, source_path=str(p), label=label, role="main",
            intermediate_path=str(dest), width=cav.width, height=cav.height,
            fps=cav.fps, nb_frames=len(cav.frames))
        gproj._parsed[mid] = cav
        return mid

    gc = gproj.add_clip(add_gmedia("stack", "I" + "P" * 9), "main")   # 1 I + 9 P
    o1 = gproj.add_mosh("pframe_duplicate", {"factor": 2}, gc.id)
    o2 = gproj.add_mosh("pframe_duplicate", {"factor": 2}, gc.id)
    _check([o.id for o in gproj.clip_ops(gc.id)] == [o1.id, o2.id],
           "two ops stack on one clip, in add order", failures)
    gr = gproj.render(fake, tmp / "g.avi")
    # op1: 9P -> 18P (19 frames); op2: 18P -> 36P (37 frames)
    _check(gr["frames"] == 37,
           "stacked ops apply in sequence (9P -> 18P -> 36P, +I = 37)", failures)
    _check(gproj.move_mosh(o2.id, -1)
           and [o.id for o in gproj.clip_ops(gc.id)] == [o2.id, o1.id],
           "move_mosh reorders the stack", failures)
    _check(gproj.remove_mosh(o1.id)
           and [o.id for o in gproj.clip_ops(gc.id)] == [o2.id],
           "remove_mosh drops an op from the stack", failures)
    _check(not gproj.remove_mosh("nope"), "remove_mosh on a missing id is a no-op",
           failures)

    o3 = gproj.add_mosh("pframe_duplicate", {"factor": 2}, gc.id)      # stack of 2
    grec = gproj.bake_clip(fake, gc.id)
    _check(gproj.clip(gc.id).archived
           and all(gproj.op(o.id).archived for o in (o2, o3)),
           "bake_clip archives the clip and every op in its stack", failures)
    _check(grec.baked_clip_id in {c.id for c in gproj.clips}
           and len(gproj.clip_ops(grec.baked_clip_id)) == 0,
           "bake_clip yields a baked clip with an empty stack", failures)

    print("\nH. Parameter automation (keyframed ramps)")
    from .modes.base import _build_evaluator

    ev = _build_evaluator({"keys": [[0.0, 0.0], [1.0, 1.0]]})
    _check(abs(ev(0.0)) < 1e-9 and abs(ev(0.5) - 0.5) < 1e-9
           and abs(ev(1.0) - 1.0) < 1e-9,
           "linear evaluator ramps 0 -> 0.5 -> 1", failures)
    _check(abs(ev(-1.0)) < 1e-9 and abs(ev(2.0) - 1.0) < 1e-9,
           "evaluator clamps outside the keyframe range", failures)

    # multi-keyframe: a 3-point curve up-then-down
    mk = _build_evaluator({"keys": [[0.0, 0.0], [0.5, 1.0], [1.0, 0.0]]})
    _check(abs(mk(0.25) - 0.5) < 1e-9 and abs(mk(0.5) - 1.0) < 1e-9
           and abs(mk(0.75) - 0.5) < 1e-9,
           "multi-keyframe curve interpolates each segment", failures)
    # hold (step): value jumps at keys, no interpolation
    hold = _build_evaluator({"interp": "hold",
                             "keys": [[0.0, 0.0], [0.5, 1.0], [1.0, 0.0]]})
    _check(hold(0.25) == 0.0 and hold(0.5) == 1.0 and hold(0.75) == 1.0
           and hold(1.0) == 0.0, "hold interp steps between keys", failures)
    # smooth: eased, but still hits the keys exactly and 0.5 at the midpoint
    sm = _build_evaluator({"interp": "smooth", "keys": [[0.0, 0.0], [1.0, 1.0]]})
    _check(abs(sm(0.0)) < 1e-9 and abs(sm(1.0) - 1.0) < 1e-9
           and abs(sm(0.5) - 0.5) < 1e-9 and sm(0.25) < 0.25,
           "smooth interp eases in/out (slower at the ends)", failures)
    # per-keyframe easing: hold for the first segment, linear for the second
    pk = _build_evaluator({"keys": [[0.0, 0.0, "hold"],
                                    [0.5, 1.0, "linear"], [1.0, 0.0]]})
    _check(pk(0.25) == 0.0 and abs(pk(0.75) - 0.5) < 1e-9,
           "per-keyframe easing mixes hold + linear segments", failures)

    vals = {"factor": {"__auto__": True, "keys": [[0.0, 1], [1.0, 3]]}}
    auto = resolve_automation(vals)
    _check(vals["factor"] == 1 and "factor" in auto,
           "resolve_automation swaps the spec for its start value", failures)
    actx = MoshContext(fps=30, width=8, height=8, automation=auto, n_frames=11)
    _check(actx.auto("factor", 0, 1) == 1 and actx.auto("factor", 10, 1) == 3,
           "ctx.auto evaluates across n_frames (1 -> 3)", failures)
    _check(actx.auto("missing", 5, 7) == 7,
           "ctx.auto returns the default for an un-automated param", failures)

    hproj = Project(name="h", assets_dir=str(tmp / "assets_h"))
    hp = tmp / "h.avi"
    _synth_avi(hp, "I" + "P" * 10)                      # 1 I + 10 P
    hdest = hproj.assets_dir / "hmedia.avi"
    hdest.write_bytes(hp.read_bytes())
    hcav = parse_avi(hdest)
    hproj.media["hmedia"] = MediaItem(
        id="hmedia", source_path=str(hp), label="h", role="main",
        intermediate_path=str(hdest), width=hcav.width, height=hcav.height,
        fps=hcav.fps, nb_frames=len(hcav.frames))
    hproj._parsed["hmedia"] = hcav
    hclip = hproj.add_clip("hmedia", "main")
    hproj.add_mosh("pframe_duplicate",
                   {"factor": {"__auto__": True, "keys": [[0.0, 1], [1.0, 3]]}},
                   hclip.id)
    hr = hproj.render(fake, tmp / "h_render.avi")
    _check(11 < hr["frames"] < 31,
           "automated factor lands between constant 1x (11) and 3x (31)", failures)
    _check(hr["frames"] == 22,
           "automated pframe_duplicate(factor 1->3) is deterministic (22 frames)",
           failures)

    print("\nI. Region-scoped moshing")
    iproj = Project(name="i", assets_dir=str(tmp / "assets_i"))
    ip = tmp / "i.avi"
    _synth_avi(ip, "I" + "P" * 9)                       # 10 frames, 9 P
    idest = iproj.assets_dir / "imedia.avi"
    idest.write_bytes(ip.read_bytes())
    icav = parse_avi(idest)
    iproj.media["imedia"] = MediaItem(
        id="imedia", source_path=str(ip), label="i", role="main",
        intermediate_path=str(idest), width=icav.width, height=icav.height,
        fps=icav.fps, nb_frames=len(icav.frames))
    iproj._parsed["imedia"] = icav
    ic = iproj.add_clip("imedia", "main")
    iop = iproj.add_mosh("pframe_duplicate", {"factor": 2}, ic.id)

    _check(iproj.render(fake, tmp / "i_whole.avi")["frames"] == 19,
           "whole-clip dup: 9 P -> 18 P (19 frames)", failures)
    iop.region_start, iop.region_end = 0, 5             # frames 0-4 = I + 4 P
    _check(iproj.render(fake, tmp / "i_region.avi")["frames"] == 14,
           "region [0,5) duplicates only its 4 P-frames (14)", failures)
    _check(Project._op_region(iop, 10) == range(0, 5),
           "_op_region builds the clamped range", failures)
    iop.region_start, iop.region_end = 0, None
    _check(Project._op_region(iop, 10) is None,
           "full / open-ended region resolves to None (whole clip)", failures)
    iop.region_start, iop.region_end = 8, 3
    _check(Project._op_region(iop, 10) is None,
           "inverted region resolves to None", failures)
    iop.region_start, iop.region_end = 2, 6
    irel = Project.load(iproj.save(tmp / "i.json"))
    _check(irel.op(iop.id).region_start == 2 and irel.op(iop.id).region_end == 6,
           "op region survives JSON round-trip", failures)

    print("\nJ. Effect-stack presets")
    from . import presets as _presets
    ppath = tmp / "presets.json"
    _check(_presets.load_presets(ppath) == {},
           "a missing presets file reads as empty", failures)
    stack = [{"mode": "bitrot", "params": {"intensity": 0.4},
              "region": [5, 10], "enabled": True},
             {"mode": "pframe_duplicate", "params": {"factor": 2},
              "region": None, "enabled": False}]
    _presets.save_preset("glitchy", stack, ppath)
    _presets.save_preset("calm",
                         [{"mode": "surge", "params": {}, "region": None,
                           "enabled": True}], ppath)
    _check(_presets.preset_names(ppath) == ["calm", "glitchy"],
           "preset_names lists saved presets sorted", failures)
    _check(_presets.load_presets(ppath)["glitchy"] == stack,
           "a preset round-trips the stack (mode/params/region/enabled)", failures)
    _check(_presets.delete_preset("calm", ppath)
           and _presets.preset_names(ppath) == ["glitchy"],
           "delete_preset removes a preset", failures)
    _check(not _presets.delete_preset("nope", ppath),
           "delete_preset on a missing name returns False", failures)

    print("\nK. Pixel-domain effects")
    from .modes import available_pixel_modes, get_pixel_mode
    _check("rgb_shift" in available_pixel_modes()
           and "pixelate" in available_pixel_modes(),
           "built-in pixel effects are registered", failures)
    _check(get_pixel_mode("rgb_shift").filter(amount=6)
           == "rgbashift=rh=6:bh=-6:rv=3:bv=-3",
           "rgb_shift builds the expected FFmpeg filter string", failures)
    _check("scale=" in get_pixel_mode("pixelate").filter(block=8),
           "pixelate builds a scale-based filter", failures)
    _check(all(m in available_pixel_modes()
               for m in ("zoom", "pan", "rotate", "shake")),
           "motion-injection effects are registered", failures)
    zf = get_pixel_mode("zoom").filter_ctx({"start": 1.0, "end": 2.0},
                                           fps=24.0, nframes=24, width=160,
                                           height=120)
    _check(zf.startswith("zoompan=") and "on/23" in zf and "s=160x120" in zf,
           "zoom animates over the clip length at project geometry", failures)

    kproj = Project(name="k", assets_dir=str(tmp / "assets_k"))
    kp = tmp / "k.avi"
    _synth_avi(kp, "I" + "P" * 5)
    kdest = kproj.assets_dir / "kmedia.avi"
    kdest.write_bytes(kp.read_bytes())
    kcav = parse_avi(kdest)
    kproj.media["kmedia"] = MediaItem(
        id="kmedia", source_path=str(kp), label="k", role="main",
        intermediate_path=str(kdest), width=kcav.width, height=kcav.height,
        fps=kcav.fps, nb_frames=len(kcav.frames))
    kproj._parsed["kmedia"] = kcav
    kc = kproj.add_clip("kmedia", "main")
    _check(not kc.has_finish(), "a plain clip needs no finish pass", failures)
    kc.pixel_effects = [{"name": "rgb_shift", "params": {"amount": 5}},
                        {"name": "unknown_fx", "params": {}}]
    _check(kc.has_finish(), "a clip with pixel FX needs the finish pass", failures)
    _check(kproj._pixel_filters(kc) == ["rgbashift=rh=5:bh=-5:rv=2:bv=-2"],
           "_pixel_filters builds known filters and skips unknown ones", failures)
    kc.pixel_effects.insert(0, {"name": "zoom",
                                "params": {"start": 1.0, "end": 3.0}})
    _check("on/5" in kproj._pixel_filters(kc, nframes=6)[0],
           "_pixel_filters animates motion modes across nframes", failures)
    kc.pixel_effects.pop(0)                         # restore for the bake check

    # live optical-flow effect: a clip property (pure-Python checks only here)
    kc.flow_transfer = {"source": "kmedia", "strength": 1.5, "region_start": 2}
    _check(kc.has_finish(), "a clip with flow_transfer needs the finish pass",
           failures)
    kfrel = Project.load(kproj.save(tmp / "kflow.json"))
    _check(kfrel.clip(kc.id).flow_transfer["source"] == "kmedia"
           and kfrel.clip(kc.id).flow_transfer["region_start"] == 2,
           "flow_transfer survives JSON round-trip", failures)
    kc.flow_transfer = None                        # clear before the bake below

    kproj.add_mosh("pframe_duplicate", {"factor": 2}, kc.id)
    krec = kproj.bake_clip(fake, kc.id)
    _check([pe["name"] for pe in kproj.clip(krec.baked_clip_id).pixel_effects]
           == ["rgb_shift", "unknown_fx"],
           "bake_clip carries pixel FX onto the baked clip", failures)
    krel = Project.load(kproj.save(tmp / "k.json"))
    _check(krel.clip(krec.baked_clip_id).pixel_effects[0]["name"] == "rgb_shift",
           "pixel FX survive JSON round-trip", failures)
    _check([re["name"] for re in krel.clip(krec.baked_clip_id).raw_effects] == [],
           "bake carries an (empty) raw FX list without error", failures)

    print("\nK2. Raw FX (numpy frame processors)")
    from .modes import available_raw_modes, is_raw_mode, raw as _rawmod
    _check("pixel_sort" in available_raw_modes() and is_raw_mode("pixel_sort"),
           "pixel_sort is registered as a raw mode", failures)
    rclip = Clip(id="rc", media_id="m", track="main",
                 raw_effects=[{"name": "pixel_sort",
                               "params": {"axis": "vertical", "lo": 0.1}}])
    _check(rclip.has_finish(),
           "a clip with raw FX needs the finish pass", failures)
    _check([s["name"] for s in kproj._raw_specs(rclip)] == ["pixel_sort"],
           "_raw_specs returns known raw effects (skips unknown)", failures)
    rclip2 = Clip(id="rc2", media_id="m", track="main",
                  raw_effects=[{"name": "nope", "params": {}}])
    _check(kproj._raw_specs(rclip2) == [],
           "_raw_specs skips unknown raw effects", failures)
    _check(Clip.from_dict(rclip.to_dict()).raw_effects[0]["name"] == "pixel_sort",
           "raw FX survive a Clip JSON round-trip", failures)
    if _rawmod.available():
        import numpy as _np
        from .modes.raw import _sort_frame
        vals = [200, 50, 120, 255, 10, 90]
        row = _np.array([[[v, v, v] for v in vals]], _np.uint8).tobytes()
        asc = _sort_frame(row, 6, 1, vertical=False, by="brightness",
                          lo=0.0, hi=1.0, descending=False)
        got = list(_np.frombuffer(asc, _np.uint8).reshape(1, 6, 3)[0, :, 0])
        _check(got == sorted(vals),
               "pixel_sort orders a fully-banded line by brightness", failures)
        band = _sort_frame(row, 6, 1, vertical=False, by="brightness",
                           lo=40 / 255, hi=210 / 255, descending=False)
        bg = list(_np.frombuffer(band, _np.uint8).reshape(1, 6, 3)[0, :, 0])
        _check(bg[3] == 255 and bg[4] == 10,
               "out-of-band pixels stay anchored (contiguous-span sort)", failures)
        from .modes.raw import blend_masked
        orig = _np.full((4, 4, 3), 10, _np.uint8).tobytes()
        proc = _np.full((4, 4, 3), 200, _np.uint8).tobytes()
        wht = blend_masked([orig], [proc], 4, 4, {"source": "luma", "lo": 0.0, "hi": 0.0})
        blk = blend_masked([orig], [proc], 4, 4, {"source": "luma", "lo": 1.0, "hi": 1.0})
        _check(_np.frombuffer(wht[0], _np.uint8)[0] > 190
               and _np.frombuffer(blk[0], _np.uint8)[0] < 20,
               "an fx_mask gates raw effects (white=FX, black=original)", failures)
    else:
        print("  [skip] numpy not installed; pixel_sort math not exercised")

    print("\nK3. Masking (layer + FX mattes)")
    from .ffmpeg import MASK_SOURCES, MASK_MODES, mask_chain
    _check(MASK_SOURCES == ("luma", "alpha", "motion", "chroma")
           and MASK_MODES == ("confine", "source"),
           "mask sources (luma/alpha/motion/chroma) + modes registered", failures)
    _check(mask_chain({"source": "luma", "lo": 0.0, "hi": 1.0})
           .startswith("format=gray,lutyuv="),
           "luma matte builds a gray + lutyuv ramp", failures)
    _check("tblend=all_mode=difference" in mask_chain({"source": "motion"}),
           "motion matte uses a frame-difference", failures)
    _check("alphaextract" in mask_chain({"source": "alpha"}),
           "alpha matte extracts the alpha plane", failures)
    _check("colorkey=0x00FF00" in mask_chain({"source": "chroma", "key": "#00ff00"}),
           "chroma matte keys the chosen color", failures)
    mc = mask_chain({"source": "luma", "invert": True, "feather": 4})
    _check("255-val" in mc and "gblur=sigma=4" in mc,
           "invert + feather extend the matte chain", failures)
    mclip = Clip(id="mc", media_id="m", track="main",
                 layer_mask={"source": "motion", "lo": 0.1, "hi": 0.6},
                 fx_mask={"source": "luma", "invert": True})
    mrt = Clip.from_dict(mclip.to_dict())
    _check(mrt.layer_mask["source"] == "motion" and mrt.fx_mask["invert"] is True,
           "layer/FX mattes survive a Clip JSON round-trip", failures)

    print("\nL. Compositing tracks & nested sequences")
    from .project import MAIN_TRACK_ID, MOTION_TRACK_ID
    lproj = Project(name="l", assets_dir=str(tmp / "assets_l"))
    _check([t.id for t in lproj.video_tracks(lproj.root_seq_id)] == [MAIN_TRACK_ID]
           and lproj.track(MOTION_TRACK_ID).role == "motion",
           "fresh project has the root main video track + motion pool", failures)
    lp = tmp / "l.avi"
    _synth_avi(lp, "I" + "P" * 5)
    ldest = lproj.assets_dir / "lmedia.avi"
    ldest.write_bytes(lp.read_bytes())
    lcav = parse_avi(ldest)
    lproj.media["lmedia"] = MediaItem(
        id="lmedia", source_path=str(lp), label="l", role="main",
        intermediate_path=str(ldest), width=lcav.width, height=lcav.height,
        fps=lcav.fps, nb_frames=len(lcav.frames))
    lproj._parsed["lmedia"] = lcav
    lproj.add_clip("lmedia", "main")
    v2 = lproj.add_track()                          # a second video track on top
    vclip = lproj.add_clip("lmedia", v2.id)
    vclip.opacity, vclip.blend_mode, vclip.gain = 0.5, "screen", 0.25
    _check([t.id for t in lproj.video_tracks(lproj.root_seq_id)]
           == [MAIN_TRACK_ID, v2.id],
           "added video track stacks above main by index", failures)
    _check(lproj.clip(vclip.id).seq_id == lproj.root_seq_id,
           "a clip is stamped with its track's sequence", failures)
    lrel = Project.load(lproj.save(tmp / "l.json"))
    rc = lrel.clip(vclip.id)
    _check(abs(rc.opacity - 0.5) < 1e-9 and rc.blend_mode == "screen"
           and abs(rc.gain - 0.25) < 1e-9,
           "clip opacity/blend/gain survive JSON round-trip", failures)
    _check([t.id for t in lrel.video_tracks(lrel.root_seq_id)]
           == [MAIN_TRACK_ID, v2.id],
           "tracks survive JSON round-trip in compositing order", failures)

    seq = lproj.add_sequence("inner")              # a precomp
    svt = lproj.video_tracks(seq.id)[0]
    d_empty = lproj._sequence_digest(seq.id)
    pclip = lproj.add_sequence_clip("main", seq.id)
    smedia = lproj.sequence_media(seq.id)
    _check(pclip.media_id == smedia.id and smedia.sequence_id == seq.id
           and smedia.derived,
           "a precomp clip links to its derived, sequence-backed media", failures)
    lproj.add_clip("lmedia", svt.id)               # fill the precomp
    _check(lproj._sequence_digest(seq.id) != d_empty,
           "a sequence's digest (cache key) changes when its clips change", failures)

    cyc = lproj.add_sequence("cyc")
    cvt = lproj.video_tracks(cyc.id)[0]
    lproj.add_sequence_clip(cvt.id, cyc.id)        # a sequence containing itself
    raised = False
    try:
        lproj.render(None, tmp / "cyc.avi", sequence_id=cyc.id)
    except ValueError as exc:
        raised = "cycle" in str(exc)
    _check(raised, "a self-referencing sequence is rejected as a cycle", failures)

    legacy = {"version": 1, "name": "old", "media": [],
              "clips": [{"id": "lc", "media_id": "lmedia", "track": "main"}],
              "mosh_ops": [], "bake_records": []}
    mproj = Project.from_dict(legacy)              # pre-compositing save format
    _check(mproj.track(MAIN_TRACK_ID).role == "video"
           and mproj.track(MOTION_TRACK_ID).role == "motion"
           and mproj.clip("lc").seq_id == mproj.root_seq_id,
           "a legacy project migrates to root sequence + main/motion tracks",
           failures)

    print()
    if failures:
        print(f"SELFTEST FAILED: {len(failures)} check(s) failed")
        return 1
    print("SELFTEST PASSED: all checks green")
    return 0


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="moshit",
                                description="Standalone datamoshing engine.")
    p.add_argument("--version", action="version", version=f"moshit {__version__}")
    p.add_argument("--ffmpeg", default=None, help="path to ffmpeg binary")
    p.add_argument("--ffprobe", default=None, help="path to ffprobe binary")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("probe", help="show ffmpeg capabilities")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("modes", help="list available mosh modes")
    sp.set_defaults(func=cmd_modes)

    sp = sub.add_parser("mosh", help="mosh a base clip with a motion source")
    sp.add_argument("--base", required=True, help="base clip (pixels held)")
    sp.add_argument("--motion", default=None, help="motion source clip")
    sp.add_argument("--mode", default="motion_splice")
    sp.add_argument("--param", action="append", help="mode param key=value (repeatable)")
    sp.add_argument("--out", required=True, help="output moshed .avi")
    sp.add_argument("--export", default=None,
                    help="also export: h264_mp4|h265_mp4|prores_mov|ffv1_mkv|vp9_webm")
    sp.add_argument("--export-out", default=None)
    sp.add_argument("--hwaccel", default=None, help="e.g. vaapi (H.264/H.265 export)")
    _add_engine_opts(sp)
    sp.set_defaults(func=cmd_mosh)

    sp = sub.add_parser("flow",
                        help="appearance-free optical-flow motion transfer (opencv)")
    sp.add_argument("--base", required=True, help="appearance source (held pixels)")
    sp.add_argument("--motion", required=True, help="motion source (drives the flow)")
    sp.add_argument("--out", required=True, help="output warped .avi")
    sp.add_argument("--strength", type=float, default=1.0, help="displacement scale")
    sp.add_argument("--preset", default="fast",
                    choices=["ultrafast", "fast", "medium"], help="DIS flow preset")
    sp.add_argument("--follow", action="store_true",
                    help="warp each base frame instead of holding the first")
    sp.add_argument("--no-accumulate", action="store_true",
                    help="use instantaneous flow (no drifting accumulation)")
    sp.add_argument("--export", default=None,
                    help="also export: h264_mp4|h265_mp4|prores_mov|ffv1_mkv|vp9_webm")
    sp.add_argument("--export-out", default=None)
    sp.add_argument("--hwaccel", default=None)
    _add_engine_opts(sp)
    sp.set_defaults(func=cmd_flow)

    sp = sub.add_parser("demo-project",
                        help="end-to-end non-destructive project demo")
    sp.add_argument("--base", required=True)
    sp.add_argument("--motion", required=True)
    sp.add_argument("--out-dir", required=True)
    _add_engine_opts(sp)
    sp.set_defaults(func=cmd_demo_project)

    sp = sub.add_parser("render-project", help="render a saved project JSON")
    sp.add_argument("project", help="path to project .json")
    sp.add_argument("--out", required=True, help="output moshed .avi")
    sp.add_argument("--export", default=None)
    sp.add_argument("--export-out", default=None)
    sp.add_argument("--no-audio", action="store_true",
                    help="do not mux source audio into the export")
    sp.set_defaults(func=cmd_render_project)

    sp = sub.add_parser("selftest", help="pure-Python checks (no ffmpeg)")
    sp.set_defaults(func=cmd_selftest)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (FFmpegError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
