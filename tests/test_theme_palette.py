"""Tests for centralized semantic terminal colors."""

import unittest

from app import MeshtasticPassApp
from theme_palette import (
    BACKGROUND,
    DIM_BASE_ALPHA,
    DIM_BASE_QUARTER_ALPHA,
    ERROR,
    THEME_PALETTES,
    ThemePalette,
    dim_base,
    dim_base_quarter,
)


class ThemePaletteTests(unittest.TestCase):
    def test_exactly_snow_and_amber_are_defined(self) -> None:
        self.assertEqual(set(THEME_PALETTES), {"snow", "amber"})

    def test_dim_base_is_exact_textual_blend_for_every_theme(self) -> None:
        self.assertEqual(BACKGROUND, "#101010")
        self.assertEqual(DIM_BASE_ALPHA, 0.5)
        for palette in THEME_PALETTES.values():
            with self.subTest(base=palette.base):
                self.assertEqual(palette.dim, dim_base(palette.base))
                self.assertEqual(palette.dim_base, palette.dim)
                self.assertEqual(palette.error, ERROR)

    def test_every_palette_defines_all_six_semantic_tokens(self) -> None:
        for name, palette in THEME_PALETTES.items():
            with self.subTest(theme=name):
                for field in ("base", "accent", "accent2", "dim", "error", "confirm"):
                    value = getattr(palette, field)
                    self.assertIsInstance(value, str)
                    self.assertTrue(value.startswith("#"))

    def test_amber_confirm_is_not_aliased_to_accent(self) -> None:
        """Item 27: CONFIRM stays distinct even where a theme's own

        accent and confirm colors happen to differ sharply -- AMBER's
        accent is yellow, its confirm is white, and nothing in the
        palette construction lets one silently fall back to the other.
        """
        amber = THEME_PALETTES["amber"]
        self.assertNotEqual(amber.accent, amber.confirm)

    def test_snow_confirm_equals_accent_but_is_a_separate_field(self) -> None:
        """SNOW's confirm and accent share the same green -- still two

        independently-set dataclass fields, not one aliased to the
        other (item 27: "Do not alias CONFIRM to ACCENT in code").
        """
        snow = THEME_PALETTES["snow"]
        self.assertEqual(snow.accent, snow.confirm)
        fields = ThemePalette.__dataclass_fields__
        self.assertIn("accent", fields)
        self.assertIn("confirm", fields)
        self.assertNotEqual(fields["accent"], fields["confirm"])

    def test_neon_colors_are_saturated_not_pastel_or_muted(self) -> None:
        """Item 25: every full-strength token must be a real neon hue --

        high saturation and full/near-full lightness in HSL terms, not
        a desaturated, muted, or pastel shade. DIM is deliberately
        exempt (it is the one intentionally subdued token).
        """
        import colorsys

        for name, palette in THEME_PALETTES.items():
            for field in ("base", "accent", "accent2", "error", "confirm"):
                value = getattr(palette, field)
                r = int(value[1:3], 16) / 255
                g = int(value[3:5], 16) / 255
                b = int(value[5:7], 16) / 255
                _h, l, s = colorsys.rgb_to_hls(r, g, b)
                with self.subTest(theme=name, field=field, value=value):
                    # White/near-white confirm/base tokens are valid
                    # "neon terminal white" even though saturation is
                    # necessarily near zero for a true white -- only
                    # enforce the saturation floor on hued colors.
                    if l < 0.9:
                        self.assertGreaterEqual(s, 0.6)

    def test_dim_is_the_only_intentionally_subdued_token(self) -> None:
        """DIM must actually be dimmer than its own theme's BASE --

        confirming dim_base's 50%-blend genuinely reduces intensity,
        not just changes hue (item 26).
        """
        import colorsys

        for name, palette in THEME_PALETTES.items():
            with self.subTest(theme=name):
                base_l = colorsys.rgb_to_hls(
                    int(palette.base[1:3], 16) / 255,
                    int(palette.base[3:5], 16) / 255,
                    int(palette.base[5:7], 16) / 255,
                )[1]
                dim_l = colorsys.rgb_to_hls(
                    int(palette.dim[1:3], 16) / 255,
                    int(palette.dim[3:5], 16) / 255,
                    int(palette.dim[5:7], 16) / 255,
                )[1]
                self.assertLess(dim_l, base_l)

    def test_dim_quarter_is_exact_textual_blend_for_every_theme(self) -> None:
        """PR #43 follow-up Part B: the CHAT SENDING animation's inactive

        arrow is 25% BASE-over-background -- derived via the exact same
        blend machinery as DIM (dim_base), never a hardcoded literal.
        """
        self.assertEqual(DIM_BASE_QUARTER_ALPHA, 0.25)
        for palette in THEME_PALETTES.values():
            with self.subTest(base=palette.base):
                self.assertEqual(palette.dim_quarter, dim_base_quarter(palette.base))

    def test_dim_quarter_is_weaker_than_ordinary_dim(self) -> None:
        """25%-BASE must be visually weaker (closer to background) than

        the ordinary 50% DIM token -- never a reuse of DIM itself.
        """
        import colorsys

        for name, palette in THEME_PALETTES.items():
            with self.subTest(theme=name):
                self.assertNotEqual(palette.dim_quarter, palette.dim)
                background_l = colorsys.rgb_to_hls(
                    int(BACKGROUND[1:3], 16) / 255,
                    int(BACKGROUND[3:5], 16) / 255,
                    int(BACKGROUND[5:7], 16) / 255,
                )[1]
                dim_l = colorsys.rgb_to_hls(
                    int(palette.dim[1:3], 16) / 255,
                    int(palette.dim[3:5], 16) / 255,
                    int(palette.dim[5:7], 16) / 255,
                )[1]
                dim_quarter_l = colorsys.rgb_to_hls(
                    int(palette.dim_quarter[1:3], 16) / 255,
                    int(palette.dim_quarter[3:5], 16) / 255,
                    int(palette.dim_quarter[5:7], 16) / 255,
                )[1]
                # Closer to the background's own lightness than DIM is.
                self.assertLess(
                    abs(dim_quarter_l - background_l), abs(dim_l - background_l)
                )

    def test_amber_dim_quarter_preserves_orange_hue(self) -> None:
        amber = THEME_PALETTES["amber"]
        r = int(amber.dim_quarter[1:3], 16)
        g = int(amber.dim_quarter[3:5], 16)
        b = int(amber.dim_quarter[5:7], 16)
        self.assertGreater(r, g)
        self.assertGreaterEqual(g, b)

    def test_amber_dim_is_a_real_orange_not_generic_gray(self) -> None:
        """Item 26: AMBER's DIM must preserve BASE's own orange hue --

        never fall back to a generic gray the way a naive "always dim
        toward gray" implementation might.
        """
        amber = THEME_PALETTES["amber"]
        r = int(amber.dim[1:3], 16)
        g = int(amber.dim[3:5], 16)
        b = int(amber.dim[5:7], 16)
        self.assertGreater(r, g)
        self.assertGreater(g, b)

    def test_manual_dark_theme_mappings_are_not_palette_sources(self) -> None:
        old_manual_colors = {"#2C2C2C", "#0E5F08", "#6F3D00"}
        self.assertTrue(
            old_manual_colors.isdisjoint(
                palette.dim_base for palette in THEME_PALETTES.values()
            )
        )
        css = MeshtasticPassApp.CSS.upper()
        self.assertTrue(all(color not in css for color in old_manual_colors))

    def test_no_retired_theme_names_remain_in_css(self) -> None:
        """Item 21/33: WHITE/GREEN/ORANGE are fully retired -- only

        SNOW/AMBER may appear as theme-scoped CSS class selectors.
        """
        css = MeshtasticPassApp.CSS
        self.assertNotIn("theme-white", css)
        self.assertNotIn("theme-green", css)
        self.assertNotIn("theme-orange", css)
        self.assertIn("theme-amber", css)


if __name__ == "__main__":
    unittest.main()
