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

import random as _random
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
    automatable: bool = False       # effect honors ctx.auto(name, i) for this param

    def describe(self) -> str:
        rng = ""
        if self.kind in ("int", "float") and (self.lo is not None or self.hi is not None):
            rng = f" [{self.lo}..{self.hi}]"
        elif self.kind == "choice":
            rng = f" {{{', '.join(map(str, self.choices))}}}"
        return f"{self.name} ({self.kind}{rng}, default={self.default!r})"


def _build_evaluator(spec: Dict) -> Callable[[float], float]:
    """Turn an automation spec into ``pos(0..1) -> value`` (clamped).

    Any number of keys is supported. A key is ``[pos, value]`` or
    ``[pos, value, easing]``; the easing applies to the *segment after* that key
    -- ``"linear"`` (default), ``"smooth"`` (smoothstep), or ``"hold"`` (step).
    The curve-level ``interp`` is the default easing for keys that don't carry
    their own (keeping older 2-element specs working).
    """
    default = spec.get("interp", "linear")
    keys = sorted(
        ((float(k[0]), k[1], (k[2] if len(k) > 2 else default))
         for k in spec.get("keys", [])),
        key=lambda k: k[0])
    if not keys:
        return lambda pos: 0.0
    if len(keys) == 1:
        v = keys[0][1]
        return lambda pos: v

    def ev(pos: float) -> float:
        if pos <= keys[0][0]:
            return keys[0][1]
        if pos >= keys[-1][0]:
            return keys[-1][1]
        for (p0, v0, e0), (p1, v1, _e1) in zip(keys, keys[1:]):
            if p0 <= pos < p1:                # exclusive upper -> clean steps
                if e0 == "hold":
                    return v0
                t = (pos - p0) / (p1 - p0) if p1 > p0 else 0.0
                if e0 == "smooth":
                    t = t * t * (3 - 2 * t)
                return v0 + (v1 - v0) * t
        return keys[-1][1]

    return ev


def random_params(mode: "MoshMode", current: Optional[Dict[str, Any]] = None,
                  rng=_random) -> Dict[str, Any]:
    """A randomised value for each of *mode*'s parameters that has a range or
    choices; params without one (and ``clip_ref``s) keep their current/default
    value. *rng* is injectable so callers can seed it for reproducibility.

    Shared by the GUI's per-effect 'randomise' button and the effect dialog."""
    values = dict(mode.defaults())
    if current:
        values.update({k: v for k, v in current.items() if k in values})
    for p in mode.params:
        has_range = p.lo is not None and p.hi is not None
        if p.kind == "int" and has_range:
            values[p.name] = rng.randint(int(p.lo), int(p.hi))
        elif p.kind == "float" and has_range:
            values[p.name] = round(rng.uniform(float(p.lo), float(p.hi)), 2)
        elif p.kind == "bool":
            values[p.name] = rng.random() < 0.5
        elif p.kind == "choice" and p.choices:
            values[p.name] = rng.choice(list(p.choices))
        # clip_ref / range-less numeric params keep their current value
    return values


def is_automation(value: Any) -> bool:
    return isinstance(value, dict) and bool(value.get("__auto__"))


def resolve_automation(values: Dict[str, Any]) -> Dict[str, Callable[[float], float]]:
    """Replace any automated param specs in *values* (in place) with their start
    scalar, and return ``{name: evaluator}`` for the ones that were automated."""
    automation: Dict[str, Callable[[float], float]] = {}
    for name in list(values):
        v = values[name]
        if is_automation(v):
            ev = _build_evaluator(v)
            automation[name] = ev
            values[name] = ev(0.0)          # static fallback = value at the start
    return automation


@dataclass
class MoshContext:
    """Runtime context handed to :meth:`MoshMode.apply`.

    Gives a mode the geometry/fps of the timeline and read access to other
    sources by label -- notably the motion-source track for splice effects.
    ``automation`` maps a parameter name to a ``pos(0..1) -> value`` curve;
    pointwise effects read it via :meth:`auto` to vary a value across the clip.
    """

    fps: float
    width: int
    height: int
    clips: Dict[str, List[Frame]] = field(default_factory=dict)
    log: Callable[[str], None] = lambda msg: None
    automation: Dict[str, Callable[[float], float]] = field(default_factory=dict)
    n_frames: int = 0

    def get_clip(self, ref: str) -> List[Frame]:
        if ref not in self.clips:
            raise KeyError(
                f"motion source '{ref}' not found; available: "
                f"{sorted(self.clips) or '(none)'}")
        return self.clips[ref]

    def clip_labels(self) -> List[str]:
        return sorted(self.clips)

    def auto(self, name: str, i: int, default: Any = None) -> Any:
        """Value of automated param *name* at input-frame index *i* (else
        *default*). Position is ``i / (n_frames - 1)`` across what the effect
        processes, so it's robust to effects that change the frame count."""
        ev = self.automation.get(name)
        if ev is None:
            return default
        pos = (i / (self.n_frames - 1)) if self.n_frames > 1 else 0.0
        return ev(pos)


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
