"""Base classes for mosh *modes* (effects).

Every effect is a configuration of one primitive: given the frames of a region,
decide which I-frames to keep or drop and which P-frame runs to append, repeat
or substitute. A mode is therefore a pure function over a list of
:class:`~moshit.avi.Frame` objects, which makes effects easy to test and easy
for third parties to write.

Modes self-register on definition (``__init_subclass__``), and they expose a
``params`` schema so a GUI can build controls for any mode -- including
third-party ones dropped into a plugin directory -- with no GUI changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..avi import Frame

# name -> MoshMode subclass
_REGISTRY: Dict[str, type] = {}


@dataclass
class Param:
    """One user-tunable parameter; the GUI renders a control from this."""

    name: str
    kind: str                       # "int" | "float" | "bool" | "choice" | "clip_ref"
    default: Any = None
    lo: Optional[float] = None
    hi: Optional[float] = None
    choices: tuple = ()
    label: str = ""
    help: str = ""

    def describe(self) -> str:
        rng = ""
        if self.kind in ("int", "float") and (self.lo is not None or self.hi is not None):
            rng = f" [{self.lo}..{self.hi}]"
        elif self.kind == "choice":
            rng = f" {{{', '.join(map(str, self.choices))}}}"
        return f"{self.name} ({self.kind}{rng}, default={self.default!r})"


@dataclass
class MoshContext:
    """Runtime context handed to :meth:`MoshMode.apply`.

    Gives a mode the geometry/fps of the timeline and read access to other
    sources by label -- notably the motion-source track for splice effects.
    """

    fps: float
    width: int
    height: int
    clips: Dict[str, List[Frame]] = field(default_factory=dict)
    log: Callable[[str], None] = lambda msg: None

    def get_clip(self, ref: str) -> List[Frame]:
        if ref not in self.clips:
            raise KeyError(
                f"motion source '{ref}' not found; available: "
                f"{sorted(self.clips) or '(none)'}")
        return self.clips[ref]

    def clip_labels(self) -> List[str]:
        return sorted(self.clips)


class MoshMode:
    """Base class for all effects. Subclass and implement :meth:`apply`."""

    name: str = ""
    description: str = ""
    params: List[Param] = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.name:
            _REGISTRY[cls.name] = cls

    def defaults(self) -> Dict[str, Any]:
        return {p.name: p.default for p in self.params}

    def resolve(self, overrides: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Merge user overrides onto defaults, ignoring unknown keys."""
        values = self.defaults()
        known = {p.name for p in self.params}
        for key, val in (overrides or {}).items():
            if key in known:
                values[key] = val
        return values

    def apply(self, frames: List[Frame], ctx: MoshContext,
              **params) -> List[Frame]:
        """Return the moshed frame list for *frames*. Must not mutate input."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Registry access
# --------------------------------------------------------------------------- #

def register(cls: type) -> type:
    """Explicit registration decorator (subclasses also auto-register)."""
    if cls.name:
        _REGISTRY[cls.name] = cls
    return cls


def available_modes() -> List[str]:
    return sorted(_REGISTRY)


def get_mode(name: str) -> MoshMode:
    if name not in _REGISTRY:
        raise KeyError(f"unknown mode '{name}'. Available: {available_modes()}")
    return _REGISTRY[name]()


def mode_class(name: str) -> type:
    return _REGISTRY[name]
