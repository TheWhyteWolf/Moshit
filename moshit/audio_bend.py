"""RAW DATA - AUDIO: bend decoded video bytes through CDP sound transforms.

This is *databending*: a clip's decoded RGB pixels are treated as a mono 16-bit
audio stream, run through a CDP (Composer Desktop Project) sound-transformation
program, and mapped back to pixels. The audio tools were never meant for images,
so they corrupt them in vivid, unpredictable, musically-structured ways.

The bridge is deliberately simple and never fails the render:

* every byte of every frame becomes one 16-bit sample (``(b-128) * 256``), the
  whole clip concatenated into one mono stream, so the FX smear colour and
  frames into each other;
* CDP runs as a subprocess on a temp WAV;
* the result is mapped back to bytes and *length-fitted* to the clip's exact
  geometry (CDP freely changes length -- longer output is truncated, shorter is
  tiled to fill), so frame count and size are always preserved.

CDP binaries are discovered at runtime (``$MOSHIT_CDP_DIR``, else the bundled
``CDP8/NewRelease``). Without them -- or without numpy -- a bend passes the frames
through untouched, exactly like the other optional raw effects.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import List, Optional

_SR = 44100                                   # arbitrary; databending ignores time


def numpy_available() -> bool:
    try:
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


def cdp_dir() -> Optional[Path]:
    """Locate the CDP program directory, or None.

    Honours ``$MOSHIT_CDP_DIR`` first, then looks for a bundled
    ``CDP8/NewRelease`` in this file's parent chain (so it works from a checkout).
    """
    cands: List[Path] = []
    env = os.environ.get("MOSHIT_CDP_DIR")
    if env:
        cands.append(Path(env))
    here = Path(__file__).resolve()
    cands += [up / "CDP8" / "NewRelease" for up in here.parents]
    for c in cands:
        if c.is_dir():
            return c
    return None


def has_program(program: str) -> bool:
    d = cdp_dir()
    return bool(d and (d / program).exists() and os.access(d / program, os.X_OK))


def available() -> bool:
    """True if databending can run (numpy importable and CDP binaries present)."""
    return numpy_available() and cdp_dir() is not None


def _pixels_to_wav(frames: List[bytes], path: Path) -> None:
    import numpy as np
    raw = np.frombuffer(b"".join(frames), np.uint8)
    samples = ((raw.astype(np.int32) - 128) * 256).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SR)
        w.writeframes(samples.tobytes())


def _wav_to_frames(path: Path, nframes: int, width: int,
                   height: int) -> List[bytes]:
    import numpy as np
    with wave.open(str(path), "rb") as w:
        data = w.readframes(w.getnframes())
    samples = np.frombuffer(data, "<i2")
    by = np.clip(np.round(samples.astype(np.float32) / 256.0) + 128,
                 0, 255).astype(np.uint8)
    fs = int(width) * int(height) * 3
    need = int(nframes) * fs
    fitted = np.resize(by, need) if by.size else np.full(need, 128, np.uint8)
    return [fitted[i * fs:(i + 1) * fs].tobytes() for i in range(int(nframes))]


def run_cdp(program: str, argv: List[str], *, timeout: float = 180.0) -> bool:
    """Invoke a CDP program; True on a clean exit with no exception/timeout."""
    d = cdp_dir()
    if not d:
        return False
    try:
        r = subprocess.run([str(d / program), *argv], capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def bend(frames: List[bytes], width: int, height: int, *, program: str,
         mode: str, positionals: List[str], flags: List[str],
         timeout: float = 180.0) -> List[bytes]:
    """Run a CDP ``program mode in out <positionals> <flags>`` over the clip.

    ``mode`` may be several whitespace-separated tokens, for CDP programs whose
    sub-command carries a mode number before the files (e.g. ``"distshift 1"`` ->
    ``distshift distshift 1 in out ...``); single-word modes are unaffected.

    Returns the length-fitted, re-pixelated frames, or the originals unchanged if
    databending is unavailable or CDP fails (so a render never breaks).
    """
    if not frames or not available():
        return frames
    nframes = len(frames)
    work = Path(tempfile.mkdtemp(prefix="moshit_cdp_"))
    try:
        in_wav, out_wav = work / "in.wav", work / "out.wav"
        _pixels_to_wav(frames, in_wav)
        argv = (mode.split() + [str(in_wav), str(out_wav)]
                + [str(p) for p in positionals] + [str(f) for f in flags])
        if not run_cdp(program, argv, timeout=timeout) or not out_wav.exists():
            return frames                     # CDP refused this input: passthrough
        return _wav_to_frames(out_wav, nframes, width, height)
    finally:
        shutil.rmtree(work, ignore_errors=True)
