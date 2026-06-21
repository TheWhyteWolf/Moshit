"""Named effect-stack presets, persisted as JSON in the user config dir.

A preset is a list of effect dicts -- ``{mode, params, region, enabled}`` -- with
no ids (ids are assigned when the preset is applied to a clip). Presets are global
(not tied to a project) and dependency-free, so the same store backs the GUI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional


def presets_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "moshit" / "presets.json"


def load_presets(path: Optional[Path] = None) -> Dict[str, List[dict]]:
    p = Path(path) if path else presets_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(p: Path, data: Dict[str, List[dict]]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def save_preset(name: str, effects: List[dict],
                path: Optional[Path] = None) -> Dict[str, List[dict]]:
    """Add or overwrite a preset; returns the full preset table."""
    p = Path(path) if path else presets_path()
    data = load_presets(p)
    data[str(name)] = list(effects)
    _write(p, data)
    return data


def delete_preset(name: str, path: Optional[Path] = None) -> bool:
    p = Path(path) if path else presets_path()
    data = load_presets(p)
    if name not in data:
        return False
    del data[name]
    _write(p, data)
    return True


def preset_names(path: Optional[Path] = None) -> List[str]:
    return sorted(load_presets(path))
