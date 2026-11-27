"""Command line entry point: pick routes, sync them, print a readable report."""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from typing import Sequence

from . import __version__
from .configfile import DEFAULT_ENTRY, load_document, resolve_config, write_text_atomic
from .credentials import Credentials
from .models import ModelSyncError
from .presets import load_presets
from .sync import (
    DEFAULT_MAX_WORKERS,
    DEFAULT_TIMEOUT,
    RouteReport,
    SyncOptions,
    SyncOutcome,
    sync_config,
)

__all__ = [
    "build_parser",
    "color_enabled",
    "is_tty",
    "main",
    "render_report",
    "render_summary",
    "supports_ansi",
]

# --- ANSI helpers ----------------------------------------------------------

_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_RED = "\x1b[31m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_CYAN = "\x1b[36m"

#: The reference tool's grey: SGR 2 (faint). Terminals that implement faint —
#: Windows Terminal, VS Code's xterm.js, and most Unix terminals — render it as
#: a light grey, so this spelling is what keeps the report pixel-identical.
_MUTED_FAINT = "\x1b[2m"
#: A legacy conhost *ignores* SGR 2 (measured: the cell attribute stays the
#: default 0x07), so grey there has to come from bright black, which conhost
#: does render (0x08). The extra faint is harmless where it is supported.
_MUTED_CONHOST = "\x1b[2;90m"

#: Environment markers of terminals known to implement faint.
_FAINT_MARKERS = (
    "WT_SESSION",      # Windows Terminal
    "TERM_PROGRAM",    # VS Code / iTerm / Apple Terminal / vscode-server
    "COLORTERM",
    "ANSICON",
    "ConEmuANSI",
    "TERM",            # xterm-compatible ptys
)

#: Style name -> ANSI code. An empty name means "leave the terminal default".
#: ``muted`` is resolved per run by :func:`grey_code`.
_STYLES: dict[str, str] = {
    "": "",
    "bold": _BOLD,
    "red": _RED,
    "green": _GREEN,
    "yellow": _YELLOW,
    "cyan": _CYAN,
}


def grey_code(environ: dict | None = None) -> str:
    """Return the escape that renders grey on *this* terminal.

    Windows Terminal and other faint-capable terminals get the reference
    project's ``\\x1b[2m``; a legacy conhost, which drops faint on the floor,
    gets ``\\x1b[2;90m`` (bright black) so ``[unchanged]``/``kept`` still read as
    grey instead of body text.
    """
    env = os.environ if environ is None else environ
    if any(env.get(marker) for marker in _FAINT_MARKERS):
        return _MUTED_FAINT
    return _MUTED_CONHOST


def _active_styles(environ: dict | None = None) -> dict[str, str]:
    """The style table in effect for one render (grey depends on the terminal)."""
    styles = dict(_STYLES)
    styles["muted"] = grey_code(environ)
    return styles


#: One piece of a report line: ``(text, style)``.
Segment = tuple[str, str]


def _paint(text: str, style: str, styles: dict[str, str] | None = None) -> str:
    """Wrap *text* in the ANSI code for *style* (a no-op for the default)."""
    table = _active_styles() if styles is None else styles
    code = table[style]
    return f"{code}{text}{_RESET}" if code else text


def _join(
    segments: Sequence[Segment],
    *,
    color: bool,
    styles: dict[str, str] | None = None,
) -> str:
    """Render one segment list, colouring only the labelled spans."""
    if not color:
        return "".join(text for text, _style in segments)
    table = _active_styles() if styles is None else styles
    return "".join(_paint(text, style, table) for text, style in segments)


def is_tty(stream=None) -> bool:
    """Whether the stream is an interactive terminal."""
    target = sys.stdout if stream is None else stream
    try:
        return bool(target.isatty())
    except (AttributeError, ValueError):
        return False


#: ``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` in the Windows console mode bitmask.
_ENABLE_VT = 0x0004


def _console_mode_probe(handle, kernel32, wt) -> bool:
    """Shared VT check for one console *handle* (the syscall part only)."""
    mode = wt.DWORD(0)
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        # Not a real conhost handle -> an emulated terminal that renders ANSI
        # natively (VS Code integrated terminal, xterm, ...). Trust it.
        return True
    if mode.value & _ENABLE_VT:
        return True
    return bool(kernel32.SetConsoleMode(handle, mode.value | _ENABLE_VT))


def _windows_vt100_ready(kernel32=None, handle=None) -> bool:
    """Best-effort check that stdout's console can render ANSI (VT100) codes.

    Ported from ``copilot-byok-sync`` so both tools behave identically in
    ``cmd.exe``/``conhost`` and in Windows Terminal:

    * a *real* console handle was probed with ``GetConsoleMode``: the
      ``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` (0x0004) bit already on -> True;
      off -> ``SetConsoleMode`` turns it on (True on success, False on a very
      old OS that cannot do VT). **This is what makes plain ``conhost`` render
      colour instead of dumping raw escape codes.**
    * a *non-real* console handle (``GetConsoleMode`` fails) is an
      ANSI-emulating pty, which already renders ANSI -> True.
    * any ``ctypes`` problem degrades to False: no colour, never a crash.

    ``kernel32``/``handle`` are test seams; the production path always uses
    ``GetStdHandle(STD_OUTPUT_HANDLE)`` with explicit ``argtypes``/``restype``.
    Declaring those signatures is essential: without them a 64-bit console
    handle is truncated to ``int``, ``GetConsoleMode`` fails, and the probe
    wrongly "trusts" a console that cannot render escapes.
    """
    if os.name != "nt":
        return True
    if kernel32 is not None:
        try:
            return _console_mode_probe(
                handle if handle is not None else 1, kernel32, _wintypes()
            )
        except Exception:
            return False
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return False
    try:
        wt = _wintypes()
        kernel32 = windll.kernel32
        kernel32.GetStdHandle.argtypes = [wt.DWORD]
        kernel32.GetStdHandle.restype = wt.HANDLE
        kernel32.GetConsoleMode.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
        kernel32.GetConsoleMode.restype = wt.BOOL
        kernel32.SetConsoleMode.argtypes = [wt.HANDLE, wt.DWORD]
        kernel32.SetConsoleMode.restype = wt.BOOL
        std_out = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        return _console_mode_probe(std_out, kernel32, wt)
    except Exception:
        return False


def _wintypes():
    """``ctypes.wintypes``, imported lazily so the module stays portable."""
    import ctypes.wintypes as wt

    return wt


def supports_ansi(stream=None, *, kernel32=None, handle=None) -> bool:
    """Whether ANSI escapes will actually be *rendered* rather than dumped.

    The probe always inspects the process's stdout console handle, exactly like
    the reference tool; on non-Windows platforms it is a no-op.
    """
    return _windows_vt100_ready(kernel32=kernel32, handle=handle)


def color_enabled(stream=None, environ: dict | None = None) -> bool:
    """Colour only on an interactive terminal and never when ``NO_COLOR`` is set.

    Being a TTY is not enough on Windows: the terminal must also render ANSI,
    which :func:`supports_ansi` guarantees by enabling virtual-terminal
    processing on a legacy console.
    """
    env = os.environ if environ is None else environ
    if "NO_COLOR" in env:
        return False
    if not is_tty(stream):
        return False
    return supports_ansi()


# --- report ----------------------------------------------------------------


def _listing(label: str, ids: Sequence[str], style: str) -> list[Segment]:
    """``  <label> (N): a, b`` with *only* the label carrying *style*.

    Mirrors the reference tool: body text stays in the terminal's default
    colour so long id lists stay readable; the label is the scan anchor.
    """
    return [
        ("  ", ""),
        (label, style),
        (f" ({len(ids)}): {', '.join(ids)}", ""),
    ]


def report_segments(report: RouteReport) -> list[list[Segment]]:
    """Build one route's report as style-tagged segment lists."""
    if report.error:
        status, status_style = "error", "red"
    elif report.changed:
        status, status_style = "changes", "green"
    else:
        status, status_style = "unchanged", "muted"

    lines: list[list[Segment]] = [[(f"[{status}]", status_style), (" ", ""), (report.route, "bold")]]

    if report.error == "no endpoint url":
        lines.append([("  warning: no endpoint url", "yellow")])
    elif report.error:
        lines.append([("  ", ""), ("error:", "red"), (f" {report.error}", "")])
    elif report.unchanged:
        lines.append([("  ", ""), ("kept", "muted"), (f" {report.kept} models", "")])
    else:
        if report.added:
            lines.append(_listing("added", report.added, "green"))
        if report.removed:
            lines.append(_listing("removed", report.removed, "red"))

    if report.filtered:
        lines.append(_listing("filtered non-text", report.filtered, "yellow"))
    if report.no_text_signal:
        lines.append([("  ", ""), ("no text signal", "yellow"), (" (deletion skipped)", "")])
    elif report.deletion_skipped:
        lines.append(
            [("  ", ""), ("deletion skipped", "yellow"), (f" ({report.deletion_skipped})", "")]
        )
    if report.discarded:
        lines.append(_listing("discarded invalid entries", report.discarded, "yellow"))
    if report.credits is not None:
        lines.append([("  ", ""), ("credits:", "cyan"), (f" {report.credits}", "")])
    return lines


def render_report(report: RouteReport, *, color: bool = False) -> list[str]:
    """Render one route's report lines."""
    return [_join(line, color=color) for line in report_segments(report)]


def render_summary(reports: Sequence[RouteReport], *, color: bool = False) -> str:
    """Render the single summary line."""
    failed = [report for report in reports if report.error]
    changed = [report for report in reports if report.changed and not report.error]
    unchanged = [
        report for report in reports if not report.changed and not report.error
    ]
    added = sum(len(report.added) for report in reports)
    removed = sum(len(report.removed) for report in reports)
    parts = [
        f"{len(reports)} routes",
        f"{len(changed)} changed (+{added} -{removed})",
        f"{len(unchanged)} unchanged",
    ]
    if failed:
        parts.append(f"{len(failed)} failed")
    return _join([("Summary:", "bold"), (f" {', '.join(parts)}", "")], color=color)


def render(outcome: SyncOutcome, path: str, *, color: bool = False) -> list[str]:
    """Render the full report; only labels are coloured, body text is default."""
    lines: list[str] = []
    for report in outcome.reports:
        lines.extend(render_report(report, color=color))
    lines.append(render_summary(outcome.reports, color=color))
    lines.append(f"config: {path}")
    return lines


# --- CLI -------------------------------------------------------------------


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"必须是正整数，收到 {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"必须是正整数，收到 {value!r}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsh-byok-sync",
        description="同步 DeepSeek Harness（DSH）第三方端点（llm-pi-ai 路由）的模型列表。",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="目标配置文件（cordis.patch.yml 或 settings.yaml）；省略时自动探测",
    )
    parser.add_argument("--profile", metavar="NAME", help="profile 名（用于探测 cordis.patch.yml）")
    parser.add_argument(
        "--entry",
        metavar="ID",
        default=DEFAULT_ENTRY,
        help=f"插件行 id / 顶层键，默认 {DEFAULT_ENTRY}",
    )
    parser.add_argument("--all", action="store_true", help="同步所有路由")
    parser.add_argument(
        "--provider",
        metavar="ROUTE",
        action="append",
        dest="providers",
        default=None,
        help="只同步指定路由（可多次指定）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只显示变更，不写文件")
    parser.add_argument("--no-delete", action="store_true", help="只新增，不删除既有模型")
    parser.add_argument("--sort", action="store_true", help="按 id 字典序升序重排模型列表")
    parser.add_argument("--no-credits", action="store_true", help="跳过余额查询")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"单个请求的超时（秒），默认 {DEFAULT_TIMEOUT:g}",
    )
    parser.add_argument(
        "--workers",
        type=_positive_int,
        default=None,
        metavar="N",
        help=(
            f"并发请求数（默认按请求数自动并发，上限 {DEFAULT_MAX_WORKERS}；"
            "--workers 1 退回完全串行）"
        ),
    )
    parser.add_argument("--credentials", metavar="PATH", help="覆盖 DSH 凭据文件路径")
    parser.add_argument(
        "--list-routes", action="store_true", help="只列出可选路由后退出（不联网）"
    )
    parser.add_argument("--no-color", action="store_true", help="禁用彩色输出")
    parser.add_argument("--version", action="version", version=f"dsh-byok-sync {__version__}")
    return parser


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        path = resolve_config(args.config, args.profile)
        document = load_document(path, args.entry)
    except ModelSyncError as exc:
        return _fail(str(exc))

    if args.list_routes:
        for route in document.routes:
            print(route)
        return 0

    if args.all and args.providers:
        return _fail("--all 与 --provider 只能二选一")
    if not args.all and not args.providers:
        return _fail("必须指定 --all 或至少一个 --provider")
    selected = list(document.routes) if args.all else list(dict.fromkeys(args.providers))

    try:
        options = SyncOptions(
            no_delete=args.no_delete,
            sort=args.sort,
            timeout=args.timeout,
            use_credits=not args.no_credits,
            credentials=Credentials.load(args.credentials),
            presets=load_presets(),
            max_workers=args.workers,
        )
        outcome = sync_config(document, selected, options=options)
    except ModelSyncError as exc:
        return _fail(str(exc))

    written = False
    if outcome.new_text is not None and not args.dry_run:
        try:
            write_text_atomic(document.path, outcome.new_text, bom=document.bom)
        except ModelSyncError as exc:
            return _fail(str(exc))
        written = True

    tty = is_tty()
    # ``color_enabled`` is what actually asks the console whether it can render
    # ANSI, and enables virtual-terminal processing on a legacy conhost as a
    # side effect. Calling it here is mandatory: skipping it (and testing only
    # isatty) is what dumps raw ``[32m`` into a cmd window.
    use_color = color_enabled() and not args.no_color
    lines = render(outcome, str(document.path), color=use_color)
    for line in lines:
        print(line)

    if not tty:
        # A redirected or piped run still gets the whole report, mirrored to
        # stderr, so a script can capture it without parsing colour escapes.
        for line in lines:
            print(line, file=sys.stderr)
        if args.dry_run:
            print("dry run: 未写入文件", file=sys.stderr)
        elif written:
            print("已写入文件", file=sys.stderr)
    elif args.dry_run:
        print(
            _paint("dry run: 未写入文件", "muted") if use_color else "dry run: 未写入文件"
        )

    return 1 if outcome.failed else 0
