"""Project data model: sequences/tracks, compositing props, and back-compat
(pure model, no ffmpeg)."""
import pytest

from moshit.project import (Clip, MediaItem, Project, Sequence, Track,
                            MAIN_TRACK_ID, MOTION_TRACK_ID, ROOT_SEQ_ID)


def _media(p, mid="m", frames=10):
    p.media[mid] = MediaItem(id=mid, source_path="x", label=mid, role="main",
                             intermediate_path="x", nb_frames=frames)
    return mid


def test_fresh_project_has_root_structure():
    p = Project()
    assert p.sequence(ROOT_SEQ_ID).name
    assert p.track(MAIN_TRACK_ID).role == "video"
    assert p.track(MOTION_TRACK_ID).role == "motion"
    assert [t.id for t in p.video_tracks(ROOT_SEQ_ID)] == [MAIN_TRACK_ID]


def test_roundtrip_preserves_tracks_and_clip_props():
    p = Project()
    _media(p)
    p.tracks.append(Track(id="v2", seq_id=ROOT_SEQ_ID, name="Video 2", index=1))
    p.clips.append(Clip(id="c", media_id="m", track="v2",
                        opacity=0.5, blend_mode="screen"))
    p2 = Project.from_dict(p.to_dict())
    assert [t.id for t in p2.video_tracks(ROOT_SEQ_ID)] == [MAIN_TRACK_ID, "v2"]
    c = p2.clip("c")
    assert c.track == "v2" and c.opacity == 0.5 and c.blend_mode == "screen"
    assert c.seq_id == ROOT_SEQ_ID


def test_legacy_project_migrates():
    legacy = {                                          # pre-compositing save format
        "version": 1, "name": "old",
        "media": [{"id": "m", "source_path": "x", "label": "x", "role": "main",
                   "intermediate_path": "x", "nb_frames": 10}],
        "clips": [{"id": "c", "media_id": "m", "track": "main", "start": 0}],
        "mosh_ops": [], "bake_records": [],
    }
    p = Project.from_dict(legacy)
    assert p.track(MAIN_TRACK_ID).role == "video"       # synthesised
    assert p.track(MOTION_TRACK_ID).role == "motion"
    c = p.clip("c")
    assert c.seq_id == ROOT_SEQ_ID and c.opacity == 1.0  # defaults backfilled
    assert [cl.id for cl in p.main_clips()] == ["c"]    # still on the main track


def test_unknown_keys_are_ignored():
    c = Clip.from_dict({"id": "c", "media_id": "m", "track": "main",
                        "bogus_field": 7})              # tolerant deserialisation
    assert c.id == "c" and not hasattr(c, "bogus_field")


def test_add_sequence_clip_links_to_backing_media():
    p = Project()
    seq = p.add_sequence("inner")
    assert p.video_tracks(seq.id)                       # has a default video track
    clip = p.add_sequence_clip("main", seq.id)
    media = p.sequence_media(seq.id)
    assert clip.media_id == media.id and media.sequence_id == seq.id
    assert media.derived and clip.seq_id == ROOT_SEQ_ID  # clip lives on root's main


def test_precomp_cycle_detected():
    # a sequence that contains itself must fail cleanly (no engine needed: the
    # cycle is caught before any rendering)
    p = Project()
    a = p.add_sequence("A")
    vt = p.video_tracks(a.id)[0]
    p.add_sequence_clip(vt.id, a.id)                    # A contains A
    with pytest.raises(ValueError, match="cycle"):
        p.render(None, "x.avi", sequence_id=a.id)
