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
                 preset: str = "fast", use_opencl: bool = True) -> List[bytes]:
    """Warp *base_frames* by the optical flow of *motion_frames*.

    Frames are RGB24 bytes (``width*height*3`` each). The result has one frame
    per motion frame: frame 0 is the unwarped base, and frame ``t`` is the base
    warped by the flow accumulated through motion frame ``t``.

    * ``hold`` -- warp the base's first frame throughout (the held "melt", like
      motion_splice holding its keyframe); else warp the t-th base frame.
    * ``accumulate`` -- sum flow over time (drifting smear) vs. instantaneous.
    * ``strength`` -- scale the displacement.
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

    out = [np.ascontiguousarray(base[0], np.uint8)]
    for t in range(1, len(motion)):
        gray = cv2.cvtColor(motion[t], cv2.COLOR_RGB2GRAY)
        flow = get(dis.calc(um(prev_gray), um(gray), None))
        if flow.shape[:2] != (h, w):
            flow = cv2.resize(flow, (w, h))
        acc = acc + flow * float(strength) if accumulate else flow * float(strength)
        mapx = gx + acc[..., 0]
        mapy = gy + acc[..., 1]
        src = base[0] if hold else base[min(t, len(base) - 1)]
        warped = get(cv2.remap(um(src), um(mapx), um(mapy), cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_REFLECT))
        out.append(np.ascontiguousarray(warped, np.uint8))
        prev_gray = gray

    return [f.tobytes() for f in out]
