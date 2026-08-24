"""Tests for the reproducible uConsole launcher installer."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
INSTALLER = PROJECT_DIR / "install-launcher.sh"


class LauncherInstallerTests(unittest.TestCase):
    def test_installer_is_idempotent_and_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            home = Path(temporary_home)
            config_home = home / ".config"
            data_home = home / ".local" / "share"
            labwc_dir = config_home / "labwc"
            labwc_dir.mkdir(parents=True)
            labwc_config = labwc_dir / "rc.xml"
            labwc_config.write_text(
                "<?xml version=\"1.0\"?>\n"
                "<labwc_config>\n"
                "  <keyboard><default /></keyboard>\n"
                "</labwc_config>\n",
                encoding="utf-8",
            )

            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "XDG_CONFIG_HOME": str(config_home),
                    "XDG_DATA_HOME": str(data_home),
                }
            )
            environment.pop("LABWC_PID", None)

            for _ in range(2):
                subprocess.run(
                    [str(INSTALLER)],
                    cwd=PROJECT_DIR,
                    env=environment,
                    check=True,
                    capture_output=True,
                    text=True,
                )

            profile = (
                config_home / "lxterminal" / "lxterminal-meshtasticpass.conf"
            ).read_text(encoding="utf-8")
            self.assertIn("fontname=Monospace 13", profile)
            self.assertIn("hidemenubar=true", profile)

            labwc = labwc_config.read_text(encoding="utf-8")
            self.assertIn("<keyboard><default /></keyboard>", labwc)
            self.assertEqual(labwc.count("BEGIN MeshtasticPass launcher"), 1)
            self.assertIn('title="MeshtasticPass*"', labwc)
            self.assertIn('name="ToggleFullscreen"', labwc)

            launcher = (home / ".local/bin/meshtasticpass-launch").read_text(
                encoding="utf-8"
            )
            runner = (home / ".local/bin/meshtasticpass-run").read_text(
                encoding="utf-8"
            )
            desktop = (
                data_home / "applications" / "meshtasticpass.desktop"
            ).read_text(encoding="utf-8")

            self.assertIn("--no-remote", launcher)
            self.assertIn("--profile=meshtasticpass", launcher)
            self.assertNotIn("--simulate", launcher)
            self.assertIn('"$PROJECT_DIR/app.py"', runner)
            self.assertNotIn("--simulate", runner)
            self.assertIn("Name=MeshtasticPass", desktop)
            self.assertIn("Categories=Utility;", desktop)


if __name__ == "__main__":
    unittest.main()
