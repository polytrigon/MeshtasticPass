"""Central semantic colors for MeshtasticPass terminal themes.

Three user-selectable themes exist: SNOW, AMBER and MATRIX (see
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
# subdued red anywhere in any palette. DIM is the only intentionally
# subdued token, and it is DERIVED (see dim_base), never hand-picked.
NEON_RED = "#FF1744"
# ERROR is identical -- the same neon red -- in every theme, so it is
# also exposed as one theme-independent constant for CSS/markup call
# sites that have no other reason to branch on the current theme.
ERROR = NEON_RED

THEME_PALETTES = {
    # SNOW: a bright-white terminal. ACCENT is neon green, ACCENT2 is a
    # bright electric violet/purple (#B84DFF -- real-hardware follow-up
    # to the original #9D00FF: same ~276 deg blue-violet hue family
    # -- unmistakably purple, not pink/magenta, not blue -- but
    # lightness raised from 0.5 to ~0.65 at full saturation for better
    # visibility on the physical uConsole display, still nowhere near
    # pastel). CONFIRM equals ACCENT's green (still a distinct semantic
    # token in code -- see ThemePalette/module docstring).
    "snow": _palette(
        base="#F2F2F2",
        accent="#39FF14",
        accent2="#B84DFF",
        error=NEON_RED,
        confirm="#39FF14",
    ),
    # AMBER: a neon-orange terminal. ACCENT is neon light blue, ACCENT2 is
    # neon yellow, and CONFIRM is bright white -- deliberately NOT aliased
    # to ACCENT's blue, so a successful operation always reads as "white",
    # independent of AMBER's own accent hue.
    "amber": _palette(
        base="#FF8C00",
        accent="#40C4FF",
        accent2="#FFEA00",
        error=NEON_RED,
        confirm="#F2F2F2",
    ),
    # MATRIX: a phosphor-green terminal. BASE is the CRT green the theme is
    # named for; because DIM, the MESH dot grid and the CHAT sending
    # animation's weak arrow are all DERIVED from BASE (see dim_base/
    # dim_base_quarter/grid_dot), the entire ambient surface of the app --
    # every stale, passive and background element -- goes green on its own.
    # The theme's identity lives there, not in its accents.
    #
    # The accents therefore have to LEAVE the green family to stay legible
    # against it. ACCENT is the focus/selection token and so takes the
    # largest hue separation available (~150 deg, electric violet); a
    # green-adjacent ACCENT would make focus the hardest thing on screen to
    # locate, which is the one job that token has. Violet also stays clear
    # of ERROR's neon red -- a hot magenta would have sat ~30 deg from it
    # and read as "something went wrong" at a glance. ACCENT2 is neon cyan,
    # distinct from both BASE and ACCENT and consistent with AMBER's own
    # cool-blue accent. CONFIRM is bright white for AMBER's exact reason:
    # a successful operation must read as "white" independent of the
    # theme's hue, and aliasing it to BASE would make success
    # indistinguishable from ordinary text.
    "matrix": _palette(
        base="#00FF41",
        accent="#D400FF",
        accent2="#00E5FF",
        error=NEON_RED,
        confirm="#F2F2F2",
    ),
}
