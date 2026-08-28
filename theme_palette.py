"""Central semantic colors for MeshtasticPass terminal themes.

Exactly two user-selectable themes exist: SNOW and AMBER (see
app_settings.COLOR_CHOICES). Each defines six semantic tokens --
BASE/ACCENT/ACCENT2/DIM/ERROR/CONFIRM -- and every widget consumes one
of these tokens, never a theme-specific literal color, so switching
themes recolors the whole app from one place. CONFIRM is always a
distinct token, even where it happens to equal ACCENT (SNOW): code
must never alias CONFIRM to ACCENT.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.color import Color


BACKGROUND = "#101010"
FAVORITE_ACCENT = "#FFD700"
# 50%-intensity BASE, over the application background -- see dim_base.
# Textual's alpha blend against an opaque background produces the exact
# RGB a real 50%-intensity rendering would show, so this single alpha
# value is what "derives DIM from BASE" means for both themes: no
# separate literal DIM color is ever hand-picked, and AMBER's DIM is
# never a generic gray -- it is BASE's own orange, halved.
DIM_BASE_ALPHA = 0.5
# A CHAT SENDING animation's inactive arrow (see app.py's
# SendingArrowAnimation/DELIVERY_CHECKMARKS) is deliberately weaker
# than the ordinary 50%-intensity DIM token above -- 25% BASE-over-
# background, derived via the exact same blend machinery, never a
# hand-picked gray or a reuse of DIM_BASE_ALPHA.
DIM_BASE_QUARTER_ALPHA = 0.25
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
    """Resolve BASE at 50% opacity: stale/passive information."""
    return _blend_over_background(base, DIM_BASE_ALPHA)


def dim_base_quarter(base: str) -> str:
    """Resolve BASE at 25% opacity: a CHAT SENDING animation's inactive arrow."""
    return _blend_over_background(base, DIM_BASE_QUARTER_ALPHA)


def grid_dot(base: str) -> str:
    """Resolve BASE at ~10% opacity: the subtle MESH background dot grid."""
    return _blend_over_background(base, GRID_DOT_ALPHA)


@dataclass(frozen=True)
class ThemePalette:
    """Six semantic tokens. Widgets consume these, never a literal color."""

    base: str
    accent: str
    accent2: str
    dim: str
    error: str
    confirm: str
    grid_dot: str
    dim_quarter: str
    favorite_accent: str = FAVORITE_ACCENT

    @property
    def dim_base(self) -> str:
        """Back-compatible alias for `dim` -- see the token itself."""
        return self.dim


def _palette(
    base: str, accent: str, accent2: str, error: str, confirm: str
) -> ThemePalette:
    return ThemePalette(
        base,
        accent,
        accent2,
        dim_base(base),
        error,
        confirm,
        grid_dot(base),
        dim_base_quarter(base),
    )


# High-saturation, terminal-readable "neon" colors only -- no desaturated
# colors, muted earth tones, dusty orange, gray-green, pastel yellow, or
# subdued red anywhere in either palette. DIM is the only intentionally
# subdued token, and it is DERIVED (see dim_base), never hand-picked.
NEON_RED = "#FF1744"
# ERROR is identical -- the same neon red -- in both SNOW and AMBER, so
# it is also exposed as one theme-independent constant for CSS/markup
# call sites that have no other reason to branch on the current theme.
ERROR = NEON_RED

THEME_PALETTES = {
    # SNOW: a bright-white terminal. ACCENT is neon green, ACCENT2 is a
    # bright neon violet/purple (#9D00FF -- "Electric Violet": pure,
    # fully-saturated blue-violet hue, brighter/higher-blue than the
    # standard "dark violet" #9400D3, unmistakably purple rather than
    # drifting toward magenta/hot pink), CONFIRM equals ACCENT's green
    # (still a distinct semantic token in code -- see ThemePalette/
    # module docstring).
    "snow": _palette(
        base="#F2F2F2",
        accent="#39FF14",
        accent2="#9D00FF",
        error=NEON_RED,
        confirm="#39FF14",
    ),
    # AMBER: a neon-orange terminal. ACCENT is neon yellow, ACCENT2 is
    # neon light blue, and CONFIRM is bright white -- deliberately NOT
    # aliased to ACCENT's yellow, so a successful operation always reads
    # as "white", independent of AMBER's own accent hue.
    "amber": _palette(
        base="#FF8C00",
        accent="#FFEA00",
        accent2="#40C4FF",
        error=NEON_RED,
        confirm="#F2F2F2",
    ),
}
