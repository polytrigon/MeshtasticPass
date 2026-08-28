"""Standalone script for the process-level shutdown smoke test.

See tests/test_shutdown.py, which launches this as a real subprocess
and asserts it exits promptly on a normal quit -- WITHOUT sending it
SIGINT/SIGKILL -- because that is the only way to genuinely exercise
the interpreter-level shutdown path a real `python app.py` invocation
goes through. MeshtasticPassApp.run() (via Textual's own App.run())
calls asyncio.run() under the hood, and asyncio.run()'s own shutdown
sequence unconditionally waits for every job ever submitted to the
event loop's default executor before the interpreter may exit at all
(see BaseEventLoop.shutdown_default_executor()) -- an in-process
unittest run (via IsolatedAsyncioTestCase) does not drive the loop the
same way a real script does, so it cannot prove this class of hang is
actually fixed; only a real subprocess can.

Usage: python3 _exit_probe.py [--stuck-auto-sync]
Exits 0 on a clean, prompt shutdown.
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import MeshtasticPassApp  # noqa: E402
from app_settings import AppSettings  # noqa: E402
from simulated_radio_service import SimulatedRadioService  # noqa: E402


async def _quit_soon(pilot) -> None:
    await pilot.pause()
    await pilot.press("f4")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings = AppSettings.load(
            config_path=Path(tmp_dir) / "config.json",
            profile_path=Path(tmp_dir) / "profile",
        )
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        if "--stuck-auto-sync" in sys.argv:
            # A clock-set call that never returns -- the exact failure
            # shape (a genuinely stalled SDK admin call) the daemon-
            # thread fix (see MeshtasticPassApp._run_radio_worker) must
            # tolerate without blocking process exit.
            settings.set_clock_auto_sync(True)
            radio.sync_clock = lambda: threading.Event().wait()

        app = MeshtasticPassApp(radio, settings)
        app.run(headless=True, auto_pilot=_quit_soon)


if __name__ == "__main__":
    main()
