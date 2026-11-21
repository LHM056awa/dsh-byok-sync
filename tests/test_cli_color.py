"""Tests for the terminal colour capability checks in :mod:`dsh_byok_sync.cli`.

The interesting case is Windows: being a TTY is not enough, because a legacy
``conhost`` window dumps raw ``\\x1b[32m`` unless the process enables
virtual-terminal processing first. These tests pin that behaviour down without
touching a real console.
"""

from __future__ import annotations

import ctypes
import io
import os
import sys
import unittest
from unittest.mock import patch

try:  # `python -m unittest discover -s tests`
    import _helpers  # noqa: F401  (side effect: puts src/ on sys.path)
except ImportError:  # pragma: no cover
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import _helpers  # noqa: F401

from dsh_byok_sync import cli

VT = 0x0004


class _FakeKernel32:
    """Minimal stand-in for the two console-mode calls the probe makes."""

    def __init__(self, *, is_console=True, mode=0, can_set=True):
        self.is_console = is_console
        self.mode = mode
        self.can_set = can_set
        self.set_calls: list[int] = []

    @staticmethod
    def _mode_of(pointer) -> ctypes.c_ulong:
        return ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ulong)).contents

    def GetConsoleMode(self, handle, pointer):  # noqa: N802 - mirrors Win32
        if not self.is_console:
            return 0
        self._mode_of(pointer).value = self.mode
        return 1

    def SetConsoleMode(self, handle, mode):  # noqa: N802
        self.set_calls.append(mode)
        if not self.can_set:
            return 0
        self.mode = mode
        return 1


class _TtyStream(io.StringIO):
    def isatty(self) -> bool:  # noqa: D401 - test double
        return True


class _WindowsOnly(unittest.TestCase):
    def setUp(self):
        if os.name != "nt":
            self.skipTest("the VT probe only exists on Windows")


class VtProbeTest(_WindowsOnly):
    """The syscall part of the probe, driven through a stub kernel32."""

    def _probe(self, kernel):
        return cli._windows_vt100_ready(kernel32=kernel, handle=11)

    def test_vt_already_enabled_does_not_call_set(self):
        kernel = _FakeKernel32(mode=VT)
        self.assertTrue(self._probe(kernel))
        self.assertEqual([], kernel.set_calls)

    def test_vt_disabled_is_switched_on(self):
        """This is what makes a legacy conhost render colour instead of `[32m`."""
        kernel = _FakeKernel32(mode=0)
        self.assertTrue(self._probe(kernel))
        self.assertEqual([VT], kernel.set_calls)
        self.assertTrue(kernel.mode & VT)

    def test_old_os_that_refuses_vt_reports_no_colour(self):
        kernel = _FakeKernel32(mode=0, can_set=False)
        self.assertFalse(self._probe(kernel))

    def test_handle_that_is_not_a_console_is_trusted(self):
        """Pipes, redirected files and ANSI-native ptys cannot be probed."""
        kernel = _FakeKernel32(is_console=False)
        self.assertTrue(self._probe(kernel))
        self.assertEqual([], kernel.set_calls)


class SupportsAnsiTest(unittest.TestCase):
    def test_non_windows_is_always_capable(self):
        with patch.object(cli.os, "name", "posix"):
            self.assertTrue(cli.supports_ansi())

    def test_console_handle_is_probed(self):
        if os.name != "nt":
            self.skipTest("Windows-only branch")
        kernel = _FakeKernel32(mode=0)
        self.assertTrue(cli.supports_ansi(kernel32=kernel))
        self.assertEqual([VT], kernel.set_calls)

    def test_kernel32_failure_degrades_to_plain_text(self):
        if os.name != "nt":
            self.skipTest("Windows-only branch")

        class Boom:
            def GetConsoleMode(self, *args):  # noqa: N802
                raise RuntimeError("kaboom")

        self.assertFalse(cli.supports_ansi(kernel32=Boom()))


class ColorEnabledMatrixTest(unittest.TestCase):
    def test_no_color_wins(self):
        stream = _TtyStream()
        self.assertFalse(cli.color_enabled(stream, {"NO_COLOR": ""}))

    def test_plain_stream_is_never_coloured(self):
        self.assertFalse(cli.color_enabled(io.StringIO(), {}))

    def test_tty_with_ansi_capability_is_coloured(self):
        with patch.object(cli, "supports_ansi", return_value=True):
            self.assertTrue(cli.color_enabled(_TtyStream(), {}))

    def test_tty_without_ansi_capability_stays_plain(self):
        """A console that cannot enable VT must never receive escapes."""
        with patch.object(cli, "supports_ansi", return_value=False):
            self.assertFalse(cli.color_enabled(_TtyStream(), {}))

    def test_is_tty_false_when_isatty_raises(self):
        class Broken:
            def isatty(self):
                raise ValueError("closed")

        self.assertFalse(cli.is_tty(Broken()))

    def test_is_tty_false_when_stream_lacks_isatty(self):
        self.assertFalse(cli.is_tty(object()))


class GreyCodeTest(unittest.TestCase):
    """The grey spelling must match the reference where faint is supported."""

    def test_faint_capable_terminal_gets_the_reference_bytes(self):
        self.assertEqual("\x1b[2m", cli.grey_code({"WT_SESSION": "3c1b-..."}))
        self.assertEqual("\x1b[2m", cli.grey_code({"TERM_PROGRAM": "vscode"}))

    def test_legacy_conhost_gets_bright_black_instead(self):
        """conhost drops SGR 2, so grey has to come from bright black there."""
        self.assertEqual("\x1b[2;90m", cli.grey_code({}))

    def test_render_uses_faint_when_the_terminal_supports_it(self):
        from dsh_byok_sync.sync import RouteReport

        with patch.object(cli, "grey_code", return_value="\x1b[2m"):
            lines = cli.render_report(RouteReport(route="r", kept=2), color=True)
        self.assertEqual("\x1b[2m[unchanged]\x1b[0m \x1b[1mr\x1b[0m", lines[0])
        self.assertEqual("  \x1b[2mkept\x1b[0m 2 models", lines[1])


class ReportColourSpansTest(unittest.TestCase):
    """Only the label spans carry colour; body text stays in the default colour.

    That is how the reference tool renders, and it keeps 100-model id lists
    readable instead of painting them red/green.
    """

    def _lines(self, report, color=True):
        from dsh_byok_sync.cli import render_report

        return render_report(report, color=color)

    def test_headline_colours_only_the_bracket_tag(self):
        from dsh_byok_sync.sync import RouteReport

        lines = self._lines(RouteReport(route="QianwenAI", kept=163))
        self.assertEqual("\x1b[2;90m[unchanged]\x1b[0m \x1b[1mQianwenAI\x1b[0m", lines[0])

    def test_kept_label_is_grey_but_the_count_is_not(self):
        from dsh_byok_sync.sync import RouteReport

        lines = self._lines(RouteReport(route="QianwenAI", kept=163))
        self.assertEqual("  \x1b[2;90mkept\x1b[0m 163 models", lines[1])

    def test_unchanged_tag_is_not_a_bare_faint_code(self):
        """A bare ``\\x1b[2m`` is ignored by a legacy conhost (it renders as body
        text); grey must come from bright-black so ``[unchanged]`` looks the same
        in cmd and in Windows Terminal."""
        from dsh_byok_sync.sync import RouteReport

        line = self._lines(RouteReport(route="r", kept=1))[0]
        self.assertIn("\x1b[90m", line.replace("\x1b[2;90m", "\x1b[90m"))
        self.assertNotEqual("\x1b[2m[unchanged]\x1b[0m \x1b[1mr\x1b[0m", line)

    def test_filtered_label_is_yellow_and_ids_stay_plain(self):
        from dsh_byok_sync.sync import RouteReport

        lines = self._lines(
            RouteReport(route="r", filtered=("a-video-1", "b-embed"), changed=True)
        )
        self.assertEqual("  \x1b[33mfiltered non-text\x1b[0m (2): a-video-1, b-embed", lines[1])

    def test_added_and_removed_colour_only_the_label(self):
        from dsh_byok_sync.sync import RouteReport

        lines = self._lines(
            RouteReport(route="iris", added=("m1", "m2"), removed=("old",), changed=True)
        )
        self.assertEqual("  \x1b[32madded\x1b[0m (2): m1, m2", lines[1])
        self.assertEqual("  \x1b[31mremoved\x1b[0m (1): old", lines[2])

    def test_change_headline_is_green_and_route_bold(self):
        from dsh_byok_sync.sync import RouteReport

        lines = self._lines(RouteReport(route="iris", changed=True))
        self.assertEqual("\x1b[32m[changes]\x1b[0m \x1b[1miris\x1b[0m", lines[0])

    def test_error_tag_and_label_are_red_message_is_plain(self):
        from dsh_byok_sync.sync import RouteReport

        lines = self._lines(RouteReport(route="r", error="HTTP 401"))
        self.assertEqual("\x1b[31m[error]\x1b[0m \x1b[1mr\x1b[0m", lines[0])
        self.assertEqual("  \x1b[31merror:\x1b[0m HTTP 401", lines[1])

    def test_credits_label_is_cyan(self):
        from dsh_byok_sync.sync import RouteReport

        lines = self._lines(RouteReport(route="r", credits="CNY 1.00", changed=True))
        self.assertEqual("  \x1b[36mcredits:\x1b[0m CNY 1.00", lines[1])

    def test_summary_bolds_only_its_label(self):
        from dsh_byok_sync.cli import render_summary
        from dsh_byok_sync.sync import RouteReport

        line = render_summary([RouteReport(route="r", kept=1)], color=True)
        self.assertEqual("\x1b[1mSummary:\x1b[0m 1 routes, 0 changed (+0 -0), 1 unchanged", line)

    def test_plain_mode_keeps_exactly_the_same_text(self):
        from dsh_byok_sync.sync import RouteReport

        report = RouteReport(
            route="QianwenAI",
            kept=163,
            filtered=("a-video-1",),
            credits="CNY 1.00",
        )
        self.assertEqual(
            [
                "[unchanged] QianwenAI",
                "  kept 163 models",
                "  filtered non-text (1): a-video-1",
                "  credits: CNY 1.00",
            ],
            self._lines(report, color=False),
        )
        for line in self._lines(report, color=True):
            self.assertTrue(line.startswith("\x1b[") or line.startswith("  "))


class MainColourWiringTest(unittest.TestCase):
    """The real ``main`` path must consult the console, not just ``isatty``.

    Regression guard: an earlier version computed the colour decision inline
    (``tty and not --no-color``), so the VT-enabling probe never ran during a
    real sync and cmd/conhost kept showing raw ``[32m`` sequences.
    """

    def _run_main(self, argv, *, stdout):
        from pathlib import Path

        from dsh_byok_sync.configfile import Document
        from dsh_byok_sync.sync import SyncOutcome

        document = Document(
            path=Path("config.yml"),
            text="",
            kind="patch",
            entry_id="llm-pi-ai",
            newline="\n",
            bom=False,
            routes={},
        )
        # This suite must not inherit a host NO_COLOR (some harnesses set it),
        # otherwise every colour assertion would trivially pass as plain text.
        env = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
        with patch.dict(os.environ, env, clear=True), patch.object(
            cli, "resolve_config", return_value=Path("config.yml")
        ), patch.object(cli, "load_document", return_value=document), patch.object(
            cli, "sync_config", return_value=SyncOutcome((), None)
        ), patch.object(cli, "Credentials") as credentials, patch.object(
            cli, "load_presets", return_value=()
        ), patch.object(sys, "stdout", stdout):
            credentials.load.return_value = object()
            return cli.main(list(argv))

    def test_probe_is_asked_when_stdout_is_a_tty(self):
        stream = _TtyStream()
        with patch.object(cli, "supports_ansi", return_value=True) as probe:
            code = self._run_main(["--all", "--dry-run"], stdout=stream)
        self.assertEqual(0, code)
        probe.assert_called_once()

    def test_no_colour_when_the_probe_says_the_console_cannot_render(self):
        stream = _TtyStream()
        with patch.object(cli, "supports_ansi", return_value=False):
            self._run_main(["--all", "--dry-run"], stdout=stream)
        self.assertNotIn("\x1b", stream.getvalue())

    def test_colour_is_emitted_when_the_probe_agrees(self):
        stream = _TtyStream()
        with patch.object(cli, "supports_ansi", return_value=True):
            self._run_main(["--all", "--dry-run"], stdout=stream)
        self.assertIn("\x1b[", stream.getvalue())

    def test_no_color_flag_forces_plain_text(self):
        stream = _TtyStream()
        with patch.object(cli, "supports_ansi", return_value=True):
            self._run_main(["--all", "--dry-run", "--no-color"], stdout=stream)
        self.assertNotIn("\x1b", stream.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
