"""Discovery of mode plugins.

Built-in modes ship in this package; user modes can be dropped as ``.py`` files
into a plugin directory (default ``~/.config/moshit/modes``) and will appear
as first-class effects. A broken plugin is reported and skipped rather than
crashing the application.

Note: a mode file is ordinary Python, so loading a third-party mode runs its
code. That is fine for your own machine; treat installing someone else's mode
as you would installing any script.
"""
from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import sys
import traceback
from pathlib import Path
from typing import List, Optional

from . import base

_BUILTIN_SKIP = {"base", "loader"}


def default_user_dir() -> Path:
    root = Path(__file__).resolve().parent
    import os
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base_dir = Path(xdg) if xdg else Path.home() / ".config"
    return base_dir / "moshit" / "modes"


def _load_builtins() -> List[str]:
    loaded = []
    package = sys.modules[__package__]
    for info in pkgutil.iter_modules(package.__path__):
        if info.name in _BUILTIN_SKIP:
            continue
        try:
            importlib.import_module(f"{__package__}.{info.name}")
            loaded.append(info.name)
        except Exception:
            sys.stderr.write(f"[modes] failed to load built-in '{info.name}':\n")
            traceback.print_exc()
    return loaded


def _load_dir(path: Path) -> List[str]:
    loaded = []
    if not path.is_dir():
        return loaded
    for py in sorted(path.glob("*.py")):
        if py.name.startswith("_"):
            continue
        mod_name = f"moshit_usermode_{py.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, py)
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            loaded.append(py.stem)
        except Exception:
            sys.stderr.write(f"[modes] failed to load plugin '{py}':\n")
            traceback.print_exc()
    return loaded


def load_modes(user_dirs: Optional[List[Path]] = None) -> List[str]:
    """Load built-in and user modes; return the registry's mode names."""
    _load_builtins()
    dirs = list(user_dirs) if user_dirs else [default_user_dir()]
    for d in dirs:
        _load_dir(Path(d))
    return base.available_modes()
