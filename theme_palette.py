"""Central semantic colors for MeshtasticPass terminal themes."""

from __future__ import annotations

from dataclasses import dataclass

from textual.color import Color


BACKGROUND = "#101010"
ERROR = "#FF1744"
FAVORITE_ACCENT = "#FFD700"
DIM_BASE_ALPHA = 0.35
GRID_DOT_ALPHA = 0.10


def _blend_over_background(base: str, alpha: float) -> str:
    """Resolve BASE at the given opacity over the application background.

    Terminal colors are opaque, so Textual's RGB blend produces the exact
    color that the requested alpha composition would display over #101010.
    """
    background = Color.parse(BACKGROUND)
    resolved = background.blend(Color.parse(base), alpha)
    return resolved.hex.upper()


def dim_base(base: str) -> str:
    """Resolve BASE at 35% opacity: stale/passive information."""
    return _blend_over_background(base, DIM_BASE_ALPHA)


def grid_dot(base: str) -> str:
    """Resolve BASE at ~10% opacity: the subtle MESH background dot grid."""
    return _blend_over_background(base, GRID_DOT_ALPHA)


@dataclass(frozen=True)
class ThemePalette:
    base: str
    accent: str
    dim_base: str
    grid_dot: str
    error: str = ERROR
    favorite_accent: str = FAVORITE_ACCENT


def _palette(base: str, accent: str) -> ThemePalette:
    return ThemePalette(base, accent, dim_base(base), grid_dot(base))


THEME_PALETTES = {
    "white": _palette("#D8D8D8", "#39FF14"),
    "green": _palette("#39FF14", "#FF8C00"),
    "orange": _palette("#FF8C00", "#D8D8D8"),
}
