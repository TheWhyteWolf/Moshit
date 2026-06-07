"""Minimal, dependency-free AVI (RIFF) reader/writer for datamoshing.

We move video through MPEG-4 Part 2 in an AVI container because:

* AVI stores every frame as a discrete RIFF chunk, so frames can be identified,
  reordered, deleted or duplicated as raw byte ranges; and
* the container tolerates a stream that does not begin on a keyframe, which is
  exactly the state a datamosh produces.

Frames are classified by reading the MPEG-4 *VOP* (Video Object Plane) coding
type straight from the bitstream. The bitstream is authoritative; the AVI index
is only used as a cross-check, because it may be absent or inconsistent.

This module has no third-party dependencies.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #

def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _p32(value: int) -> bytes:
    return struct.pack("<I", value & 0xFFFFFFFF)


AVIIF_KEYFRAME = 0x10

# MPEG-4 Part 2 start codes.
_VOP_START = b"\x00\x00\x01\xb6"          # Video Object Plane
_VOP_TYPES = {0: "I", 1: "P", 2: "B", 3: "S"}


def classify_vop(data: bytes) -> str:
    """Return 'I', 'P', 'B' or 'S' from the first VOP in *data*, else '?'.

    The two bits immediately following the 4-byte VOP start code are
    ``vop_coding_type`` (ISO/IEC 14496-2). VOL/VOS headers that may precede the
    VOP in a keyframe chunk are skipped automatically because we search for the
    VOP start code rather than assuming it is first.
    """
    i = data.find(_VOP_START)
    if i < 0 or i + 4 >= len(data):
        return "?"
    return _VOP_TYPES.get((data[i + 4] >> 6) & 0x03, "?")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Frame:
    """A single coded frame: the payload of one ``NNdc`` chunk."""

    data: bytes
    coding_type: str = "?"        # 'I', 'P', 'B', 'S' or '?'
    stream_id: bytes = b"00dc"
    source: str = ""              # provenance label (originating clip)

    @property
    def is_iframe(self) -> bool:
        return self.coding_type == "I"

    @property
    def is_pframe(self) -> bool:
        return self.coding_type == "P"

    @property
    def is_bframe(self) -> bool:
        return self.coding_type == "B"

    @property
    def size(self) -> int:
        return len(self.data)

    def copy(self, **overrides) -> "Frame":
        return Frame(
            data=overrides.get("data", self.data),
            coding_type=overrides.get("coding_type", self.coding_type),
            stream_id=overrides.get("stream_id", self.stream_id),
            source=overrides.get("source", self.source),
        )


@dataclass
class AviVideo:
    """A parsed, video-only AVI: reusable header bytes plus a list of frames.

    ``hdrl`` is the original ``LIST hdrl`` chunk verbatim. On write we reuse it
    and patch only the two frame-count fields, so dimensions, codec fourcc and
    frame rate are preserved exactly.
    """

    hdrl: bytearray
    frames: List[Frame]
    width: int
    height: int
    fps: float
    video_ckid: bytes
    _avih_total_off: int          # offset of avih.dwTotalFrames within hdrl
    _strh_len_off: int            # offset of video strh.dwLength within hdrl
    source: str = ""

    def __len__(self) -> int:
        return len(self.frames)

    @property
    def iframe_indices(self) -> List[int]:
        return [i for i, f in enumerate(self.frames) if f.is_iframe]

    @property
    def pframe_count(self) -> int:
        return sum(1 for f in self.frames if f.is_pframe)

    def summary(self) -> str:
        kinds = "".join(f.coding_type for f in self.frames)
        return (f"{self.width}x{self.height} @ {self.fps:.3f}fps, "
                f"{len(self.frames)} frames [{kinds[:80]}"
                f"{'...' if len(kinds) > 80 else ''}]")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _iter_chunks(buf: bytes, start: int, end: int):
    """Yield ``(ckid, data_offset, size)`` for RIFF chunks in ``[start, end)``."""
    pos = start
    while pos + 8 <= end:
        ckid = bytes(buf[pos:pos + 4])
        size = _u32(buf, pos + 4)
        data_off = pos + 8
        if data_off + size > len(buf):           # truncated/garbage tail
            break
        yield ckid, data_off, size
        pos = data_off + size
        if size & 1:                              # chunks pad to even length
            pos += 1


def _is_video_ckid(ckid: bytes, video_ckid: Optional[bytes]) -> bool:
    if len(ckid) != 4 or ckid[2:4] not in (b"dc", b"db"):
        return False
    if video_ckid and ckid[0:2] != video_ckid[0:2]:
        return False
    return True


def _collect_movi(buf: bytes, start: int, end: int,
                  out: List[Tuple[bytes, int, int]]) -> None:
    """Collect frame chunks from a ``movi`` list, descending into ``rec `` groups."""
    for ckid, off, size in _iter_chunks(buf, start, end):
        if ckid == b"LIST":                       # e.g. a 'rec ' interleave group
            _collect_movi(buf, off + 4, off + size, out)
        else:
            out.append((ckid, off, size))


def _parse_strl(buf: bytes, start: int, end: int, index: int):
    """Parse one ``strl``. Returns (ckid|None, w, h, fps, strh_len_off)."""
    fcc_type = None
    width = height = 0
    fps = 0.0
    strh_len_off = -1
    is_video = False
    for ckid, off, size in _iter_chunks(buf, start, end):
        if ckid == b"strh":
            fcc_type = bytes(buf[off:off + 4])
            scale = _u32(buf, off + 20)           # dwScale
            rate = _u32(buf, off + 24)            # dwRate
            strh_len_off = off + 32               # dwLength
            if fcc_type == b"vids":
                is_video = True
                if scale:
                    fps = rate / scale
        elif ckid == b"strf" and is_video and size >= 40:
            width = _u32(buf, off + 4)            # biWidth
            h = _u32(buf, off + 8)                # biHeight (may be top-down/negative)
            height = abs(h if h < 0x8000_0000 else h - 0x1_0000_0000)
    if is_video:
        return (("%02d" % index).encode("ascii") + b"dc",
                width, height, fps, strh_len_off)
    return None, 0, 0, 0.0, -1


def _parse_hdrl(buf: bytes, start: int, end: int):
    """Parse ``hdrl``. Returns absolute offsets, dimensions and fps."""
    video_ckid = None
    width = height = 0
    fps = 0.0
    avih_total_off = -1
    strh_len_off = -1
    stream_index = 0
    for ckid, off, size in _iter_chunks(buf, start, end):
        if ckid == b"avih":
            us_per_frame = _u32(buf, off + 0)     # dwMicroSecPerFrame
            avih_total_off = off + 16             # dwTotalFrames
            width = _u32(buf, off + 32)           # dwWidth
            height = _u32(buf, off + 36)          # dwHeight
            if us_per_frame:
                fps = 1_000_000.0 / us_per_frame
        elif ckid == b"LIST" and bytes(buf[off:off + 4]) == b"strl":
            ck, w, h, f, sloff = _parse_strl(buf, off + 4, off + size, stream_index)
            if ck is not None and video_ckid is None:
                video_ckid = ck
                width = w or width
                height = h or height
                fps = f or fps
                strh_len_off = sloff
            stream_index += 1
    return video_ckid, width, height, fps, avih_total_off, strh_len_off


def _parse_idx1_keyframes(buf: bytes, off: int, size: int,
                          video_ckid: Optional[bytes]) -> List[bool]:
    """Return an ordered list of keyframe flags for video entries in idx1."""
    flags: List[bool] = []
    for i in range(size // 16):
        e = off + i * 16
        ckid = bytes(buf[e:e + 4])
        if _is_video_ckid(ckid, video_ckid):
            flags.append(bool(_u32(buf, e + 4) & AVIIF_KEYFRAME))
    return flags


def parse_avi(path) -> AviVideo:
    """Parse a video-only AVI into an :class:`AviVideo`."""
    buf = bytearray(Path(path).read_bytes())
    if buf[0:4] != b"RIFF" or buf[8:12] != b"AVI ":
        raise ValueError(f"{path}: not a RIFF/AVI file")
    riff_end = min(8 + _u32(buf, 4), len(buf))

    hdrl: Optional[bytearray] = None
    avih_total_off = strh_len_off = -1
    movi_chunks: List[Tuple[bytes, int, int]] = []
    video_ckid: Optional[bytes] = None
    width = height = 0
    fps = 0.0
    idx1_keyframes: List[bool] = []

    for ckid, off, size in _iter_chunks(buf, 12, riff_end):
        if ckid == b"LIST":
            ltype = bytes(buf[off:off + 4])
            if ltype == b"hdrl":
                hdrl_start = off - 8              # include 'LIST' + size dword
                hdrl = bytearray(buf[hdrl_start:off + size])
                (video_ckid, width, height, fps,
                 a_abs, s_abs) = _parse_hdrl(buf, off + 4, off + size)
                avih_total_off = a_abs - hdrl_start if a_abs >= 0 else -1
                strh_len_off = s_abs - hdrl_start if s_abs >= 0 else -1
            elif ltype == b"movi":
                _collect_movi(buf, off + 4, off + size, movi_chunks)
        elif ckid == b"idx1":
            idx1_keyframes = _parse_idx1_keyframes(buf, off, size, video_ckid)

    if hdrl is None:
        raise ValueError(f"{path}: no 'hdrl' header list found")
    if video_ckid is None:
        video_ckid = b"00dc"

    frames: List[Frame] = []
    for ckid, off, size in movi_chunks:
        if _is_video_ckid(ckid, video_ckid):
            data = bytes(buf[off:off + size])
            frames.append(Frame(data=data, coding_type=classify_vop(data),
                                stream_id=ckid, source=str(path)))

    # Cross-check / fill unknowns using the index keyframe flags.
    if idx1_keyframes and len(idx1_keyframes) == len(frames):
        for fr, is_key in zip(frames, idx1_keyframes):
            if fr.coding_type == "?":
                fr.coding_type = "I" if is_key else "P"

    return AviVideo(
        hdrl=hdrl, frames=frames, width=width, height=height, fps=fps,
        video_ckid=video_ckid, _avih_total_off=avih_total_off,
        _strh_len_off=strh_len_off, source=str(path),
    )


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

def write_avi(path, frames: List[Frame], template: AviVideo) -> None:
    """Write a video-only AVI from *frames*, reusing ``template.hdrl``.

    The original header is reused verbatim with only the two frame-count fields
    patched, so geometry/codec/fps stay identical. A fresh ``idx1`` index is
    built with chunk offsets relative to the ``movi`` FourCC (first entry = 4),
    which is the conventional AVI layout.
    """
    n = len(frames)
    hdrl = bytearray(template.hdrl)
    if template._avih_total_off >= 0:
        struct.pack_into("<I", hdrl, template._avih_total_off, n)
    if template._strh_len_off >= 0:
        struct.pack_into("<I", hdrl, template._strh_len_off, n)

    movi = bytearray(b"movi")
    idx1 = bytearray()
    for f in frames:
        ckid = f.stream_id if len(f.stream_id) == 4 else template.video_ckid
        offset = len(movi)                        # position of ckid vs 'movi' FourCC
        movi += ckid
        movi += _p32(f.size)
        movi += f.data
        if f.size & 1:
            movi += b"\x00"                       # pad to even length
        flags = AVIIF_KEYFRAME if f.is_iframe else 0
        idx1 += ckid + _p32(flags) + _p32(offset) + _p32(f.size)

    movi_list = b"LIST" + _p32(len(movi)) + bytes(movi)
    idx1_chunk = b"idx1" + _p32(len(idx1)) + bytes(idx1)
    body = bytes(hdrl) + movi_list + idx1_chunk
    riff = b"RIFF" + _p32(4 + len(body)) + b"AVI " + body
    Path(path).write_bytes(riff)
