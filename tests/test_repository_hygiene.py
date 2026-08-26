"""Regression checks for public-repository and local-data hygiene."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app_settings import AppSettings
from chat_store import ChatStore, default_chat_db_path
from simulated_radio_service import SIMULATED_MESSAGES, SimulatedRadioService


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def repository_python_files() -> list[Path]:
    """Every tracked-shaped .py file outside ignored tool directories."""
    excluded_roots = {".git", ".venv", "venv", "__pycache__"}
    matches: list[Path] = []
    for path in PROJECT_ROOT.rglob("*.py"):
        relative = path.relative_to(PROJECT_ROOT)
        if relative.parts and relative.parts[0] in excluded_roots:
            continue
        matches.append(path)
    return matches


def repository_runtime_files() -> set[Path]:
    """Return sensitive runtime-shaped files outside ignored tool directories."""
    excluded_roots = {".git", ".venv", "venv", "__pycache__"}
    matches: set[Path] = set()
    for path in PROJECT_ROOT.rglob("*"):
        relative = path.relative_to(PROJECT_ROOT)
        if not relative.parts or relative.parts[0] in excluded_roots:
            continue
        if not path.is_file():
            continue
        name = path.name
        if (
            name == "config.json"
            or name.endswith((".db", ".db-shm", ".db-wal"))
            or ".sqlite" in name
            or name.endswith(".log")
        ):
            matches.add(relative)
    return matches


class RepositoryHygieneTests(unittest.TestCase):
    def test_gitignore_keeps_critical_private_patterns(self) -> None:
        patterns = {
            line.strip()
            for line in (PROJECT_ROOT / ".gitignore").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        required = {
            ".venv/",
            "venv/",
            "__pycache__/",
            "*.py[cod]",
            ".pytest_cache/",
            ".mypy_cache/",
            ".coverage",
            "htmlcov/",
            ".env",
            ".env.*",
            "*.pem",
            "*.key",
            "*.crt",
            "*.p12",
            "*.pfx",
            "*.db",
            "*.db-shm",
            "*.db-wal",
            "*.sqlite",
            "*.sqlite3",
            "config.json",
            "*.log",
            ".vscode/",
            ".idea/",
            ".DS_Store",
            "*~",
        }
        self.assertEqual(required - patterns, set())

    def test_default_user_paths_are_outside_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            with patch.dict(os.environ, {}, clear=True), patch.object(
                Path,
                "home",
                return_value=home,
            ):
                settings = AppSettings()
                chat_path = default_chat_db_path()

            self.assertEqual(
                settings.config_path,
                home / ".config" / "meshtasticpass" / "config.json",
            )
            self.assertEqual(
                settings.profile_path,
                home
                / ".config"
                / "lxterminal"
                / "lxterminal-meshtasticpass.conf",
            )
            self.assertEqual(
                chat_path,
                home / ".local" / "share" / "meshtasticpass" / "chat.db",
            )
            for path in (settings.config_path, settings.profile_path, chat_path):
                self.assertNotEqual(path, PROJECT_ROOT)
                self.assertNotIn(PROJECT_ROOT, path.parents)

    def test_simulated_runtime_writes_only_to_xdg_user_directories(self) -> None:
        before = repository_runtime_files()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_home = root / "config"
            data_home = root / "data"
            with patch.dict(
                os.environ,
                {
                    "XDG_CONFIG_HOME": str(config_home),
                    "XDG_DATA_HOME": str(data_home),
                },
            ):
                settings = AppSettings.load()
                settings.set_favorite("!a11ce001", True)
                settings.save()

                radio = SimulatedRadioService(
                    connect_delay=0,
                    message_interval=0,
                    scripted_messages=(),
                )
                radio.connect()
                message = SIMULATED_MESSAGES[0]
                store = ChatStore.open()
                store.add_incoming(
                    packet_id=message.packet_id,
                    node_id=message.sender_node_id,
                    sender_name=message.sender_long_name,
                    sender_short_name=message.sender_short_name,
                    channel_index=message.channel_index or 0,
                    text=message.text,
                    radio_rx_at=message.radio_rx_at,
                    received_at=1_700_000_300.0,
                )
                store.close()
                radio.close()

            self.assertEqual(
                settings.config_path,
                config_home / "meshtasticpass" / "config.json",
            )
            self.assertTrue(settings.config_path.is_file())
            self.assertTrue(
                (data_home / "meshtasticpass" / "chat.db").is_file()
            )

        self.assertEqual(repository_runtime_files(), before)

    def test_no_source_file_contains_a_lone_surrogate_code_point(self) -> None:
        """Regression for a real production crash: a \\uD8xx/\\uDFxx-range

        escape written inside a non-raw string literal (describing an
        astral emoji codepoint-by-codepoint in a docstring) is not
        combined into the intended character -- Python creates two
        separate, genuine lone surrogate code points instead. Such a
        string cannot be encoded as UTF-8, which crashed module import
        on strict-UTF-8 Linux (a uConsole in the field) with
        UnicodeEncodeError, even though the exact same file imported
        fine on macOS. There must be zero surrogate code points
        (U+D800-U+DFFF) in any project source file, full stop -- not
        worked around via encoding/errors settings.
        """
        offenders: list[str] = []
        for path in repository_python_files():
            text = path.read_text(encoding="utf-8")
            bad = [
                (index, hex(ord(character)))
                for index, character in enumerate(text)
                if 0xD800 <= ord(character) <= 0xDFFF
            ]
            if bad:
                offenders.append(
                    f"{path.relative_to(PROJECT_ROOT)}: {bad[:5]}"
                    + (" ..." if len(bad) > 5 else "")
                )
        self.assertEqual(offenders, [], "\n".join(offenders))

    def _assert_fresh_import_does_not_raise_unicode_encode_error(
        self, module_name: str
    ) -> None:
        """Import `module_name` in a brand-new subprocess and fail if that

        raises UnicodeEncodeError.

        Deliberately a fresh subprocess rather than importlib.reload() in
        this process: app.py is a live Textual App module, and reloading
        it re-executes CSS/widget-registration side effects shared with
        every other already-imported reference to it in this same test
        run, corrupting unrelated tests that hold the original class
        objects (confirmed by reproducing exactly that breakage while
        developing this test).
        """
        result = subprocess.run(
            [sys.executable, "-c", f"import {module_name}"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "PYTHONUTF8": "1", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"},
        )
        self.assertNotIn("UnicodeEncodeError", result.stderr, result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_importing_grapheme_text_does_not_raise_unicode_encode_error(
        self,
    ) -> None:
        self._assert_fresh_import_does_not_raise_unicode_encode_error("grapheme_text")

    def test_importing_app_does_not_raise_unicode_encode_error(self) -> None:
        self._assert_fresh_import_does_not_raise_unicode_encode_error("app")

    def test_app_simulate_gets_past_module_import_under_strict_utf8(self) -> None:
        """Launch the real entry point exactly as production does

        (python app.py --simulate) in a subprocess forced to strict
        UTF-8 (mirroring the uConsole's Linux environment, where the
        default was not permissive like macOS's), and confirm it gets
        past module import and actually starts driving the TUI (it
        keeps running without a real TTY, so it is deliberately
        terminated after a few seconds rather than waited on).
        """
        process = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "app.py"), "--simulate"],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONUTF8": "1", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"},
        )
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate(timeout=5)
        self.assertNotIn("UnicodeEncodeError", stderr, stderr)
        self.assertNotIn("Traceback", stderr, stderr)


if __name__ == "__main__":
    unittest.main()
