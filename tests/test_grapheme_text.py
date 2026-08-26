"""Tests for grapheme_text.install_flag_pair_protection().

MESH's existing grapheme-safety tests (tests/test_mesh_topology.py)
continue to prove grapheme_clusters()/truncate_to_cells() indirectly via
mesh_topology.compact_node_label(), which re-exports these names for
backward compatibility. This file covers the CHAT-emoji/scrollbar-
corruption fix.

An earlier version of this fix mutated the rendered string (inserting a
zero-width joiner between the two codepoints of a flag pair). That
approach was replaced: it altered rendered semantic text, and a
malformed \\uXXXX escape describing it in a docstring introduced actual
lone surrogate code points into the source, crashing on strict-UTF-8
Linux at import time. The current fix instead patches Rich's own
grapheme splitter (rich.cells.split_graphemes, used by
rich._wrap.divide_line -> rich.cells.chop_cells, the same path
Textual's Static/Content rendering uses) so it treats a regional-
indicator pair as one indivisible span -- exactly how it already treats
a ZWJ sequence or a variation-selector pair -- without ever touching
any caller's string.
"""

from __future__ import annotations

import subprocess
import sys
import unittest

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from grapheme_text import grapheme_clusters, install_flag_pair_protection


def _wrapped_lines(text: str, width: int) -> list[str]:
    """Reproduce exactly what Textual's Static uses to wrap this content."""
    console = Console(width=max(width, 1), legacy_windows=False, file=open("/dev/null", "w"))
    return [str(line) for line in Text(text).wrap(console, width)]


def _is_lone_regional_indicator(cluster: str) -> bool:
    return len(cluster) == 1 and 0x1F1E6 <= ord(cluster) < 0x1F200


class FlagPairProtectionInstalledTests(unittest.TestCase):
    """Exercised with the protection installed -- the real, always-on

    production state, since app.py installs it once at module import
    time and every other test module transitively triggers that by
    importing app.
    """

    @classmethod
    def setUpClass(cls) -> None:
        install_flag_pair_protection()

    def test_never_severs_a_flag_pair_at_any_wrap_width(self) -> None:
        text = "🇺🇸🇬🇧🇫🇷🇩🇪🇮🇹🇪🇸"
        for width in range(2, 12):
            with self.subTest(width=width):
                for line in _wrapped_lines(text, width):
                    for cluster in grapheme_clusters(line):
                        self.assertFalse(
                            _is_lone_regional_indicator(cluster),
                            msg=(
                                f"orphaned regional-indicator half "
                                f"{cluster!r} at width={width}, line={line!r}"
                            ),
                        )

    def test_zwj_family_emoji_never_severed(self) -> None:
        """Baseline this fix deliberately does not touch: Rich already

        keeps a ZWJ sequence together on its own, at every width.
        """
        family = "👨‍👩‍👧‍👦"
        text = family * 4
        for width in range(2, 10):
            with self.subTest(width=width):
                for line in _wrapped_lines(text, width):
                    for cluster in grapheme_clusters(line):
                        if cluster.strip():
                            self.assertEqual(cluster, family)

    def test_skin_tone_emoji_never_severed(self) -> None:
        waving_medium = "👋🏽"
        text = waving_medium * 4
        for width in range(2, 10):
            with self.subTest(width=width):
                for line in _wrapped_lines(text, width):
                    for cluster in grapheme_clusters(line):
                        if cluster.strip():
                            self.assertEqual(cluster, waving_medium)

    def test_cjk_wide_characters_still_wrap_within_width(self) -> None:
        text = "你好世界" * 4
        for width in range(2, 10):
            with self.subTest(width=width):
                for line in _wrapped_lines(text, width):
                    self.assertLessEqual(cell_len(line.rstrip("\n")), width)

    def test_flag_pair_cell_width_is_unchanged(self) -> None:
        """The patch changes only where wrap breaks may fall -- never

        how wide anything is reported to be.
        """
        self.assertEqual(cell_len("🇺🇸"), 2)
        self.assertEqual(cell_len("🇺🇸🇬🇧"), 4)

    def test_does_not_mutate_any_string(self) -> None:
        original = "go 🇺🇸 team 👨‍👩‍👧‍👦 👋🏽 你好"
        copy = str(original)
        _wrapped_lines(original, 5)
        self.assertEqual(original, copy)

    def test_installing_twice_is_a_safe_no_op(self) -> None:
        install_flag_pair_protection()
        install_flag_pair_protection()
        text = "🇺🇸🇬🇧🇫🇷"
        for width in range(2, 8):
            for line in _wrapped_lines(text, width):
                for cluster in grapheme_clusters(line):
                    self.assertFalse(_is_lone_regional_indicator(cluster))


class FlagPairSeveringIsARealBugWithoutTheFixTests(unittest.TestCase):
    """Proves the bug this closes is genuine, not hypothetical.

    Runs in a pristine subprocess that never imports grapheme_text or
    app, so Rich's real, unpatched wrap engine is exercised directly --
    by the time any in-process test runs, some other test module has
    almost certainly already imported app and installed the patch
    process-wide, so this cannot be demonstrated in-process.
    """

    def test_flag_pair_is_severed_by_unpatched_rich(self) -> None:
        script = (
            "from rich.console import Console\n"
            "from rich.text import Text\n"
            "def wrapped_lines(text, width):\n"
            "    console = Console(width=max(width, 1), legacy_windows=False, file=open('/dev/null', 'w'))\n"
            "    return [str(line) for line in Text(text).wrap(console, width)]\n"
            "def count_ri(line):\n"
            "    return sum(1 for ch in line if 0x1F1E6 <= ord(ch) < 0x1F200)\n"
            "found = False\n"
            "original = '\\U0001F1FA\\U0001F1F8\\U0001F1EC\\U0001F1E7\\U0001F1EB\\U0001F1F7'\n"
            "for width in range(1, 8):\n"
            "    for line in wrapped_lines(original, width):\n"
            "        if count_ri(line) % 2 == 1:\n"
            "            found = True\n"
            "print('SEVERED' if found else 'NOT_SEVERED')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "SEVERED")


if __name__ == "__main__":
    unittest.main()
