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


def test_moved_project_folder_repairs_media_paths(engine, tmp_path, make_clip):
    from pathlib import Path
    src = make_clip("s.mp4")
    proj = Project(name="t", config=engine.config,
                   assets_dir=str(tmp_path / "p_assets"))
    m = proj.import_media(engine, src)
    proj.save(tmp_path / "p.json")
    # simulate moving the project folder: json + assets relocate together,
    # leaving the absolute paths inside the json stale
    moved = tmp_path / "moved"
    moved.mkdir()
    (tmp_path / "p.json").rename(moved / "p.json")
    (tmp_path / "p_assets").rename(moved / "p_assets")

    p2 = Project.load(moved / "p.json")
    m2 = p2.media[m.id]
    assert Path(m2.intermediate_path).exists()
    assert Path(m2.intermediate_path).parent == moved / "p_assets"
    assert p2.missing_media() == []
    assert Path(p2.assets_dir) == moved / "p_assets"


def test_missing_media_detection_and_relink(engine, project, make_clip):
    from pathlib import Path
    src = make_clip("v.mp4")
    m = project.import_media(engine, src)
    assert project.missing_media() == []

    Path(m.intermediate_path).unlink()                 # go offline
    assert [x.id for x in project.missing_media()] == [m.id]

    # precomp media are excluded (they re-render from their sequence)
    project.media["pc"] = MediaItem(
        id="pc", source_path="", label="pc", role="main",
        intermediate_path="/nope.avi", sequence_id="seq1")
    assert [x.id for x in project.missing_media()] == [m.id]

    # relinking to a new source restores it in place: same id, clips attached
    clip = project.add_clip(m.id)
    src2 = make_clip("v2.mp4", color="red")
    project.relink_media(engine, m.id, src2)
    assert project.missing_media() == []
    assert Path(m.intermediate_path).exists()
    assert m.source_path == str(src2) and m.nb_frames > 0
    assert project.clip(clip.id).media_id == m.id


def test_render_reports_determinate_progress(engine, project, make_clip, tmp_path):
    m = project.import_media(engine, make_clip("p.mp4"))
    project.add_clip(m.id)
    project.add_clip(m.id)
    calls = []
    project.render(engine, tmp_path / "out.avi",
                   progress=lambda i, n, s: calls.append((i, n, s)))
    assert calls, "progress callback never fired"
    total = calls[0][1]
    assert total == 3                                  # 2 clips + assembly
    assert all(n == total for _i, n, _s in calls)
    assert [i for i, _n, _s in calls] == sorted(i for i, _n, _s in calls)
    assert calls[-1] == (2, 3, "Assembling sequence…")
