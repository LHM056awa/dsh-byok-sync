"""Shared test helpers for dsh-byok-sync.

Importing this module puts ``src/`` on ``sys.path`` so tests can
``from dsh_byok_sync import ...`` without installing the package.
"""

from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_TMP_ROOT = Path(__file__).resolve().parent / ".tmp"


def make_tmpdir() -> Path:
    """Create a fresh writable directory inside the workspace.

    ``tempfile.mkdtemp`` creates directories this machine's file sandbox
    refuses to write into, so tests use a workspace-local directory instead.
    """
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = _TMP_ROOT / uuid.uuid4().hex
    path.mkdir()
    return path


def cleanup(path: Path) -> None:
    """Best-effort removal; a sandboxed delete may legitimately fail."""
    shutil.rmtree(path, ignore_errors=True)


def write_file(path: Path, text: str, newline: str = "\n") -> Path:
    """Write ``text`` to ``path`` with explicit newline translation control."""
    data = text.replace("\n", newline) if newline != "\n" else text
    path.write_bytes(data.encode("utf-8"))
    return path
