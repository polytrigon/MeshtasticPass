"""Central semantic colors for MeshtasticPass terminal themes."""

from __future__ import annotations

from dataclasses import dataclass

from textual.color import Color


BACKGROUND = "#101010"
ERROR = "#FF1744"
DIM_BASE_ALPHA = 0.35


def dim_base(base: str) -> str:
    """Resolve BASE at 35% opacity over the application background.

    Terminal colors are opaque, so Textual's RGB blend produces the exact
    color that the requested alpha composition would display over #101010.
    """
    background = Color.parse(BACKGROUND)
    resolved = background.blend(Color.parse(base), DIM_BASE_ALPHA)
    return resolved.hex.upper()


@dataclass(frozen=True)
class ThemePalette:
    base: str
    accent: str
    dim_base: str
    error: str = ERROR


def _palette(base: str, accent: str) -> ThemePalette:
    return ThemePalette(base, accent, dim_base(base))


THEME_PALETTES = {
    "white": _palette("#D8D8D8", "#39FF14"),
    "green": _palette("#39FF14", "#FF8C00"),
    "orange": _palette("#FF8C00", "#D8D8D8"),
}
