"""Optical-flow motion transfer -- the appearance-free motion-transfer effect.

Computes dense optical flow from a *driving* clip and warps a *base* clip's
pixels by it. Because the output is only ever a *resampling* of base pixels
(``cv2.remap``), none of the driver's appearance bleeds in -- the clean
counterpart to the codec-domain :mod:`motion_splice`, whose source appearance
ghosts through.

This is the one optional, GPU-capable corner of the engine: it needs OpenCV +
numpy (``pip install 'moshit[flow]'``). OpenCV's OpenCL backend (``cv2.UMat``)
offloads the flow and the warp to the GPU -- including AMD via Mesa rusticl --
and falls back to CPU when OpenCL is unavailable.
"""
from __future__ import annotations

from typing import List

# Frame data crosses this module's boundary as raw RGB24 bytes (one frame each),
# so the engine/ffmpeg layers stay numpy-free; numpy/cv2 live only in here.


def available() -> bool:
    """True if the optional OpenCV + numpy stack is importable."""
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


def backend() -> str:
    """Human-readable compute backend, e.g. ``OpenCL: <device>`` or ``CPU``."""
    try:
        import cv2
        if cv2.ocl.haveOpenCL():
            cv2.ocl.setUseOpenCL(True)
            if cv2.ocl.useOpenCL():
                try:
                    return f"OpenCL: {cv2.ocl.Device_getDefault().name()}"
                except Exception:
                    return "OpenCL"
        return "CPU"
    except Exception:
        return "unavailable"


def _preset(name: str):
    import cv2
    return {
        "ultrafast": cv2.DISOPTICAL_FLOW_PRESET_ULTRAFAST,
        "fast": cv2.DISOPTICAL_FLOW_PRESET_FAST,
        "medium": cv2.DISOPTICAL_FLOW_PRESET_MEDIUM,
    }.get(name, cv2.DISOPTICAL_FLOW_PRESET_FAST)


def transfer_raw(base_frames: List[bytes], motion_frames: List[bytes],
                 width: int, height: int, *, hold: bool = True,
                 accumulate: bool = True, strength: float = 1.0,
                 preset: str = "fast", use_opencl: bool = True,
                 out_len=None, region=None) -> List[bytes]:
    """Warp *base_frames* by the optical flow of *motion_frames*.

    Frames are RGB24 bytes (``width*height*3`` each). Frame 0 is the unwarped
    base; frame ``i`` is the base warped by the flow accumulated through motion
    frame ``min(i, last)`` (the flow holds once the motion ends).

    * ``hold`` -- warp the base's first frame throughout (the held "melt", like
      motion_splice holding its keyframe); else warp the i-th base frame.
    * ``accumulate`` -- sum flow over time (drifting smear) vs. instantaneous.
    * ``strength`` -- scale the displacement.
    * ``out_len`` -- number of output frames (default ``len(motion)``). Pass
      ``len(base)`` to use it as a *length-preserving clip effect*.
    * ``region`` -- ``(start, end)`` output frames to warp; outside it the base
      passes through unchanged.
    """
    import cv2
    import numpy as np

    if not base_frames or not motion_frames:
        return list(base_frames)

    h, w = int(height), int(width)

    def to_arr(b: bytes):
        return np.frombuffer(b, np.uint8).reshape(h, w, 3)

    base = [to_arr(b) for b in base_frames]
    motion = [to_arr(b) for b in motion_frames]
    n_out = int(out_len) if out_len else len(motion)
    r0, r1 = region if region else (0, n_out)

    use_cl = bool(use_opencl) and cv2.ocl.haveOpenCL()
    cv2.ocl.setUseOpenCL(use_cl)

    def um(a):
        return cv2.UMat(a) if use_cl else a

    def get(a):
        return a.get() if (use_cl and isinstance(a, cv2.UMat)) else a

    dis = cv2.DISOpticalFlow_create(_preset(preset))
    gy, gx = (m.astype(np.float32) for m in np.mgrid[0:h, 0:w])
    acc = np.zeros((h, w, 2), np.float32)
    prev_gray = cv2.cvtColor(motion[0], cv2.COLOR_RGB2GRAY)

    out = []
    mi = 0                                   # how far motion has accumulated
    for i in range(n_out):
        target = min(i, len(motion) - 1)
        while mi < target:                   # advance motion flow in step with i
            mi += 1
            gray = cv2.cvtColor(motion[mi], cv2.COLOR_RGB2GRAY)
            fl = get(dis.calc(um(prev_gray), um(gray), None))
            if fl.shape[:2] != (h, w):
                fl = cv2.resize(fl, (w, h))
            acc = acc + fl * float(strength) if accumulate else fl * float(strength)
            prev_gray = gray
        src = base[0] if hold else base[min(i, len(base) - 1)]
        if not (r0 <= i < r1) or not acc.any():
            out.append(np.ascontiguousarray(src, np.uint8))   # passthrough
            continue
        warped = get(cv2.remap(um(src), um(gx + acc[..., 0]), um(gy + acc[..., 1]),
                               cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT))
        out.append(np.ascontiguousarray(warped, np.uint8))

    return [f.tobytes() for f in out]


def magnify_raw(frames: List[bytes], width: int, height: int, *,
                factor: float = 2.0, accumulate: bool = True,
                preset: str = "fast", use_opencl: bool = True) -> List[bytes]:
    """Amplify or reduce a clip's *own* motion by scaling its optical flow.

    For each frame the dense flow (instantaneous, or accumulated from frame 0 with
    ``accumulate``) is the displacement the picture's content has undergone. The
    frame is then re-warped by ``(factor - 1)`` times that displacement, so the
    motion reads as ``factor`` times larger:

    * ``factor > 1`` exaggerates movement (the "motion microscope").
    * ``factor == 1`` is identity (no warp).
    * ``factor < 1`` damps motion; ``0`` warps content back to the anchor
      positions (stabilises). Negative factors push motion the other way.

    Frame count and geometry are preserved (frame 0 always passes through), so it
    works as a length-preserving raw clip effect. RGB24 bytes in/out.
    """
    import cv2
    import numpy as np

    if len(frames) < 2:
        return list(frames)
    h, w = int(height), int(width)
    arrs = [np.frombuffer(b, np.uint8).reshape(h, w, 3) for b in frames]

    use_cl = bool(use_opencl) and cv2.ocl.haveOpenCL()
    cv2.ocl.setUseOpenCL(use_cl)

    def um(a):
        return cv2.UMat(a) if use_cl else a

    def get(a):
        return a.get() if (use_cl and isinstance(a, cv2.UMat)) else a

    dis = cv2.DISOpticalFlow_create(_preset(preset))
    gy, gx = (m.astype(np.float32) for m in np.mgrid[0:h, 0:w])
    acc = np.zeros((h, w, 2), np.float32)
    prev_gray = cv2.cvtColor(arrs[0], cv2.COLOR_RGB2GRAY)
    k = float(factor) - 1.0

    out = [np.ascontiguousarray(arrs[0], np.uint8)]   # frame 0: no motion yet
    for i in range(1, len(arrs)):
        gray = cv2.cvtColor(arrs[i], cv2.COLOR_RGB2GRAY)
        fl = get(dis.calc(um(prev_gray), um(gray), None))
        if fl.shape[:2] != (h, w):
            fl = cv2.resize(fl, (w, h))
        prev_gray = gray
        acc = acc + fl
        disp = acc if accumulate else fl
        if abs(k) < 1e-6 or not disp.any():
            out.append(np.ascontiguousarray(arrs[i], np.uint8))   # passthrough
            continue
        ex = disp * k
        # sample against the flow so content is pushed *further along* its motion
        # (factor>1 exaggerates); +ex would pull it back toward the anchor.
        warped = get(cv2.remap(um(arrs[i]), um(gx - ex[..., 0]), um(gy - ex[..., 1]),
                               cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT))
        out.append(np.ascontiguousarray(warped, np.uint8))

    return [f.tobytes() for f in out]
