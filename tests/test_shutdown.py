"""Process-level shutdown regression test.

Real-hardware report: on a uConsole, exiting MeshtasticPass normally
left a black screen with a stuck cursor -- the Python process itself
never terminated, requiring Ctrl+C. Root cause (see app.py's
MeshtasticPassApp._run_radio_worker docstring and app_controller.py's
RadioMonitor.stop): a genuinely stalled SDK admin call running under
Textual's run_worker(thread=True) is dispatched via asyncio's default
executor, and asyncio.run()'s own shutdown sequence (as used by
Textual's App.run()) unconditionally waits -- with no timeout -- for
every job that executor has ever accepted, via
BaseEventLoop.shutdown_default_executor(). Only a REAL subprocess,
driven through the genuine asyncio.run() path a real `python app.py`
invocation takes, can prove this class of hang is actually fixed --
in-process unittest runs (IsolatedAsyncioTestCase) do not exercise the
same top-level shutdown sequence.
"""

import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = Path(__file__).resolve().parent / "_exit_probe.py"
BOUND_SECONDS = 15


class ProcessLevelExitTests(unittest.TestCase):
    def _run_probe(self, *args: str) -> tuple[int, float]:
        start = time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, str(PROBE), *args],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            returncode = proc.wait(timeout=BOUND_SECONDS)
        except subprocess.TimeoutExpired:
            # Only a diagnostic cleanup for THIS test process -- never
            # part of the thing being verified. The probe itself must
            # never require an external signal to exit.
            proc.kill()
            proc.wait()
            self.fail(
                f"process did not exit within {BOUND_SECONDS}s of a normal quit "
                f"(args={args!r}) -- this reproduces the uConsole shutdown hang"
            )
        elapsed = time.monotonic() - start
        return returncode, elapsed

    def test_normal_exit_terminates_the_process_promptly(self) -> None:
        returncode, elapsed = self._run_probe()
        self.assertEqual(returncode, 0)
        self.assertLess(elapsed, BOUND_SECONDS)

    def test_exit_terminates_promptly_even_with_a_permanently_stuck_auto_sync(
        self,
    ) -> None:
        """The exact real-hardware failure shape: AUTO SYNC's own

        clock-set call never returns (a genuinely stalled SDK admin
        call). Before the daemon-thread fix, this reliably hung the
        process indefinitely (verified against the pre-fix
        implementation while developing this fix); it must not
        anymore.
        """
        returncode, elapsed = self._run_probe("--stuck-auto-sync")
        self.assertEqual(returncode, 0)
        self.assertLess(elapsed, BOUND_SECONDS)


if __name__ == "__main__":
    unittest.main()
