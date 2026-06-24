"""RAW DATA - AUDIO: the pixels<->WAV bridge (numpy) + CDP databending (gated)."""
import shutil
import wave

import pytest

from moshit import audio_bend

requires_numpy = pytest.mark.skipif(not audio_bend.numpy_available(),
                                    reason="numpy not installed")
requires_cdp = pytest.mark.skipif(not audio_bend.available(),
                                  reason="CDP binaries not available")
requires_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH")


# -- bridge (numpy only, no CDP) -------------------------------------------- #

@requires_numpy
def test_pixels_wav_roundtrip_is_lossless(tmp_path):
    import numpy as np
    W, H, N = 16, 12, 5
    frames = [np.random.default_rng(i).integers(0, 256, W * H * 3, dtype=np.uint8)
              .tobytes() for i in range(N)]
    audio_bend._pixels_to_wav(frames, tmp_path / "a.wav")
    back = audio_bend._wav_to_frames(tmp_path / "a.wav", N, W, H)
    assert back == frames                       # (b-128)*256 maps byte<->sample 1:1


@requires_numpy
def test_wav_to_frames_fits_geometry_when_length_differs(tmp_path):
    import numpy as np
    W, H, N = 8, 8, 3
    fs = W * H * 3
    short = ((np.arange(N * fs // 2, dtype=np.int32) % 256 - 128) * 256).astype("<i2")
    with wave.open(str(tmp_path / "s.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(short.tobytes())
    out = audio_bend._wav_to_frames(tmp_path / "s.wav", N, W, H)
    assert len(out) == N and all(len(f) == fs for f in out)   # tiled to exact size


@requires_numpy
def test_bend_passthrough_when_program_missing():
    import numpy as np
    frames = [np.full(8 * 8 * 3, 100, np.uint8).tobytes() for _ in range(3)]
    out = audio_bend.bend(frames, 8, 8, program="definitely_not_a_cdp_program",
                          mode="x", positionals=[], flags=[])
    assert out == frames                        # a missing/failing program is a no-op


# -- CDP databending (needs the bundled CDP binaries) ----------------------- #

@requires_cdp
def test_cdp_modes_registered_under_audio_category():
    from moshit.modes import load_modes, available_raw_modes, get_raw_mode
    load_modes()
    cdp = [m for m in available_raw_modes() if m.startswith("cdp_")]
    assert "cdp_distort_multiply" in cdp and "cdp_distort_telescope" in cdp
    assert all(get_raw_mode(m).category == "RAW DATA - AUDIO" for m in cdp)


@requires_cdp
@requires_numpy
def test_cdp_distort_corrupts_pixels_preserves_geometry():
    import numpy as np
    from moshit.modes import load_modes, get_raw_mode
    load_modes()
    W, H, N = 64, 48, 8
    frames = []
    for i in range(N):
        a = np.tile(np.linspace(0, 255, W, dtype=np.uint8),
                    (H, 1))[..., None].repeat(3, 2).copy()
        a[10:30, (2 + 2 * i):(20 + 2 * i)] = [240, 20, 20]
        frames.append(a.tobytes())
    m = get_raw_mode("cdp_distort_multiply")
    out = m.apply(list(frames), width=W, height=H, fps=24, **m.resolve({"n": 3}))
    assert len(out) == N and all(len(f) == W * H * 3 for f in out)
    assert any(o != f for o, f in zip(out, frames))     # CDP databent the pixels


@requires_cdp
@requires_ffmpeg
def test_cdp_raw_effect_renders_end_to_end(project, engine, make_clip, tmp_path,
                                           probe):
    m = project.import_media(engine, make_clip("s.mp4"), role="main")
    c = project.add_clip(m.id, "main")
    c.raw_effects = [{"name": "cdp_distort_telescope", "params": {"cyclecnt": 6}}]
    out = tmp_path / "cdp.avi"
    r = project.render(engine, out)
    assert r["frames"] == 24 and probe.dims(out) == "160x120"
