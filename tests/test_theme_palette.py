"""Tests for centralized semantic terminal colors."""

import unittest

from app import MeshtasticPassApp
from theme_palette import (
    BACKGROUND,
    DIM_BASE_ALPHA,
    ERROR,
    THEME_PALETTES,
    dim_base,
)


class ThemePaletteTests(unittest.TestCase):
    def test_dim_base_is_exact_textual_blend_for_every_theme(self) -> None:
        self.assertEqual(BACKGROUND, "#101010")
        self.assertEqual(DIM_BASE_ALPHA, 0.35)
        expected = {
            "white": ("#D8D8D8", "#565656"),
            "green": ("#39FF14", "#1E6311"),
            "orange": ("#FF8C00", "#633B0A"),
        }
        for name, (base, resolved) in expected.items():
            with self.subTest(theme=name):
                palette = THEME_PALETTES[name]
                self.assertEqual(palette.base, base)
                self.assertEqual(palette.dim_base, resolved)
                self.assertEqual(palette.dim_base, dim_base(base))
                self.assertEqual(palette.error, ERROR)

    def test_manual_dark_theme_mappings_are_not_palette_sources(self) -> None:
        old_manual_colors = {"#2C2C2C", "#0E5F08", "#6F3D00"}
        self.assertTrue(
            old_manual_colors.isdisjoint(
                palette.dim_base for palette in THEME_PALETTES.values()
            )
        )
        css = MeshtasticPassApp.CSS.upper()
        self.assertTrue(all(color not in css for color in old_manual_colors))


if __name__ == "__main__":
    unittest.main()
