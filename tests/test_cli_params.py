"""CLI --param parsing/validation (M6) and mode help-text coverage (M8).

Pure-Python: no ffmpeg needed, so these run everywhere.
"""
import pytest

from moshit.cli import _parse_params
from moshit.modes import load_modes
from moshit.modes.base import available_modes, get_mode


def setup_module(_module):
    load_modes()


def test_parse_params_coerces_types():
    p = _parse_params(["intensity=0.4", "hits=10", "seed=7"], "bitrot")
    assert p == {"intensity": 0.4, "hits": 10, "seed": 7}
    assert isinstance(p["hits"], int) and isinstance(p["intensity"], float)


def test_parse_params_bool_truthy_words():
    assert _parse_params(["keep_first=yes"], "iframe_removal")["keep_first"] is True
    assert _parse_params(["keep_first=off"], "iframe_removal")["keep_first"] is False


def test_parse_params_rejects_unknown_key():
    with pytest.raises(SystemExit) as e:
        _parse_params(["bogus=1"], "bitrot")
    assert "no parameter 'bogus'" in str(e.value)


def test_parse_params_rejects_bad_number():
    with pytest.raises(SystemExit) as e:
        _parse_params(["hits=abc"], "bitrot")
    assert "not a valid int" in str(e.value)


def test_parse_params_rejects_out_of_range():
    with pytest.raises(SystemExit) as e:
        _parse_params(["intensity=9"], "bitrot")
    assert "above the maximum" in str(e.value)
    with pytest.raises(SystemExit) as e:
        _parse_params(["intensity=-1"], "bitrot")
    assert "below the minimum" in str(e.value)


def test_parse_params_validates_choice():
    # momentum.mode is a choice param {accelerate, decelerate}
    assert _parse_params(["mode=decelerate"], "momentum")["mode"] == "decelerate"
    with pytest.raises(SystemExit) as e:
        _parse_params(["mode=sideways"], "momentum")
    msg = str(e.value)
    assert "not one of" in msg and "accelerate" in msg


def test_every_mode_param_has_help():
    """Every registered effect parameter — mosh, pixel and raw — documents
    itself, so the inspector and `moshit modes` never show a bare control (M8)."""
    import moshit.modes.pixel as px
    import moshit.modes.raw as raw
    families = [
        (available_modes(), get_mode),
        (px.available_pixel_modes(), px.get_pixel_mode),
        (raw.available_raw_modes(), raw.get_raw_mode),
    ]
    missing = [f"{name}.{p.name}"
               for names, getter in families
               for name in names
               for p in getter(name).params
               if not (p.help or "").strip()]
    assert missing == [], f"params without help: {missing}"
