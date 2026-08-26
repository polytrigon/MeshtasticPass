"""Tests for the shared grapheme-cluster-safe text helpers.

MESH's existing grapheme-safety tests (tests/test_mesh_topology.py)
continue to prove grapheme_clusters()/truncate_to_cells() indirectly via
mesh_topology.compact_node_label(), which re-exports these names for
backward compatibility. This file specifically covers
protect_flag_pairs_from_wrap_severing(), the CHAT-emoji/scrollbar-
corruption fix.

The fix's scope was determined empirically against the actually
installed Rich version's own wrap engine (rich._wrap.chop_cells ->
rich.cells.split_graphemes), not assumed: Rich already keeps a ZWJ
sequence, a skin-tone-modified emoji, and a variation-selector pair
intact through its own raw hard-wrap fallback, so those are left
completely untouched here (touching them further isn't just
unnecessary -- see test_zwj/skin_tone/cjk_are_left_untouched). The one
confirmed, reproduced gap is a regional-indicator flag pair, which is
what this transform actually changes.
"""

from __future__ import annotations

import unittest

from rich.console import Console
from rich.text import Text

from grapheme_text import grapheme_clusters, protect_flag_pairs_from_wrap_severing


def _wrapped_lines(text: str, width: int) -> list[str]:
    """Reproduce exactly what Textual's Static uses to wrap this content."""
    console = Console(width=max(width, 1), legacy_windows=False, file=open("/dev/null", "w"))
    return [str(line) for line in Text(text).wrap(console, width)]


class ProtectFlagPairsFromWrapSeveringTests(unittest.TestCase):
    def test_plain_ascii_is_unchanged(self) -> None:
        text = "hello world, this is a normal message"
        self.assertEqual(protect_flag_pairs_from_wrap_severing(text), text)

    def test_zwj_family_emoji_is_left_untouched(self) -> None:
        """Rich already protects ZWJ sequences on its own -- confirmed by

        test_zwj_family_emoji_never_severed_by_rich_directly below --
        so this function must not alter them at all.
        """
        family = "👨‍👩‍👧‍👦"
        text = f"a{family}b"
        self.assertEqual(protect_flag_pairs_from_wrap_severing(text), text)

    def test_skin_tone_modifier_is_left_untouched(self) -> None:
        waving_medium = "👋🏽"
        text = f"hi{waving_medium}!"
        self.assertEqual(protect_flag_pairs_from_wrap_severing(text), text)

    def test_cjk_wide_characters_are_left_untouched(self) -> None:
        text = "你好世界"
        self.assertEqual(protect_flag_pairs_from_wrap_severing(text), text)

    def test_single_flag_gains_an_internal_joiner(self) -> None:
        result = protect_flag_pairs_from_wrap_severing("go 🇺🇸 team")
        self.assertNotEqual(result, "go 🇺🇸 team")
        self.assertEqual(result, "go 🇺‍🇸 team")

    def test_stripping_the_joiner_restores_the_original_flag(self) -> None:
        original = "🇺🇸🇬🇧🇫🇷 flags"
        protected = protect_flag_pairs_from_wrap_severing(original)
        self.assertEqual(protected.replace("‍", ""), original)

    def test_multiple_flags_each_get_protected(self) -> None:
        result = protect_flag_pairs_from_wrap_severing("🇺🇸🇬🇧🇫🇷")
        self.assertEqual(result, "🇺‍🇸🇬‍🇧🇫‍🇷")

    def test_never_severs_a_flag_pair_at_any_wrap_width(self) -> None:
        """The actual regression: force Rich's real hard-wrap fallback

        (a run with no whitespace, wider than the target width) across
        every plausible width and confirm no line ever ends with an
        unpaired regional-indicator half.
        """
        original = "🇺🇸🇬🇧🇫🇷🇩🇪🇮🇹🇪🇸"
        protected = protect_flag_pairs_from_wrap_severing(original)
        for width in range(2, 12):
            with self.subTest(width=width):
                for line in _wrapped_lines(protected, width):
                    reconstructed = line.replace("‍", "")
                    clusters = grapheme_clusters(reconstructed)
                    for cluster in clusters:
                        self.assertFalse(
                            _is_lone_regional_indicator(cluster),
                            msg=(
                                f"orphaned regional-indicator half "
                                f"{cluster!r} at width={width}, line={line!r}"
                            ),
                        )

    def test_zwj_family_emoji_never_severed_by_rich_directly(self) -> None:
        """Establishes the baseline this fix deliberately does not touch:

        Rich's OWN wrap engine already keeps a ZWJ sequence together
        without any help, at every width, for a repeated no-whitespace
        run (the scenario that forces the raw hard-wrap fallback).
        """
        family = "👨‍👩‍👧‍👦"
        text = family * 4
        for width in range(2, 10):
            with self.subTest(width=width):
                for line in _wrapped_lines(text, width):
                    # Every non-blank cluster in every wrapped line is a
                    # complete family emoji -- never a partial one.
                    for cluster in grapheme_clusters(line):
                        if cluster.strip():
                            self.assertEqual(cluster, family)

    def test_skin_tone_emoji_never_severed_by_rich_directly(self) -> None:
        waving_medium = "👋🏽"
        text = waving_medium * 4
        for width in range(2, 10):
            with self.subTest(width=width):
                for line in _wrapped_lines(text, width):
                    for cluster in grapheme_clusters(line):
                        if cluster.strip():
                            self.assertEqual(cluster, waving_medium)

    def test_flag_pair_is_severed_without_the_fix_confirming_the_bug_is_real(
        self,
    ) -> None:
        """Sanity-check the bug this closes is genuine, not hypothetical:

        the UNPROTECTED text really does get a regional indicator
        severed by Rich's real wrap engine at some width.
        """
        original = "🇺🇸🇬🇧🇫🇷"
        found_a_split = False
        for width in range(1, 8):
            for line in _wrapped_lines(original, width):
                for cluster in grapheme_clusters(line):
                    if _is_lone_regional_indicator(cluster):
                        found_a_split = True
        self.assertTrue(found_a_split, "expected the unprotected text to split somewhere")


def _is_lone_regional_indicator(cluster: str) -> bool:
    return len(cluster) == 1 and 0x1F1E6 <= ord(cluster) < 0x1F200


if __name__ == "__main__":
    unittest.main()
