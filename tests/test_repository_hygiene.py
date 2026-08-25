"""Regression checks for public-repository and local-data hygiene."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app_settings import AppSettings
from chat_store import ChatStore, default_chat_db_path
from simulated_radio_service import SIMULATED_MESSAGES, SimulatedRadioService


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


if __name__ == "__main__":
    unittest.main()
