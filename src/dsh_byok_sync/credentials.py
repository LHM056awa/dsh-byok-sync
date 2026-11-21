"""Resolve a provider's API key from DSH's credential store.

A DSH third-party route names its credential indirectly: the profile carries
``apiKeyEnv: AGNESAI_API_KEY`` and the real secret lives elsewhere. This module
answers "what key does this route use?" without asking the user, following the
same order DSH does:

1. the **process environment** — the name in ``apiKeyEnv`` looked up in
   ``os.environ`` (or an injected mapping);
2. **``refs.<NAME>``** in ``~/.dsh/.credentials.yaml`` — the file is a
   ``version`` / ``records`` / ``refs`` document whose ``refs`` mapping is
   ``name -> plaintext key``;
3. **``records["llm-pi-ai/<route>"]``** — the record DSH's own sign-in flow
   writes; its ``payload`` carries ``apiKey`` / ``key`` / ``token`` / ``secret``.

Nothing found means "no key": the caller sends an unauthenticated request and
the endpoint's 401 is the diagnosis. Resolution never raises — a missing,
unreadable, or corrupt credential file degrades to "no key" for exactly the same
reason.

**Secrets never leave this module.** Keys are returned to the caller and used
only to build a request header; they are never printed, logged, or written
anywhere. The secret-bearing mappings are private, and both ``repr()`` and
``str()`` report counts only, so a stray ``print(credentials)``, a debugger, or
a logged traceback cannot leak one.

The YAML reader is a deliberate subset: DSH writes this file itself, in plain
block style — nested mappings of plain or quoted scalars, occasional block
sequences. Full YAML (anchors, tags, multi-document streams) is out of scope and
out of character for a machine-written file; a document using tab indentation is
refused rather than misread, because tabs make indentation ambiguous, and an
inline flow collection (``refs: {A: b}``) is *not* expanded — it is simply not a
scalar, so it contributes no reference instead of contributing a wrong one.
Using the standard library only is a project rule, hence no PyYAML.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

__all__ = ["DEFAULT_FILENAME", "Credentials", "default_path"]

#: File name of DSH's credential store inside the ``.dsh`` directory.
DEFAULT_FILENAME = ".credentials.yaml"

#: ``payload`` fields that may hold a route's key, in priority order. Compared
#: case-insensitively, because the record shape is a DSH implementation detail.
_PAYLOAD_KEY_NAMES: tuple[str, ...] = ("apikey", "key", "token", "secret")

#: Plugin id whose records hold provider credentials; ``records`` is keyed
#: ``"<plugin>/<route>"``.
_RECORD_PLUGIN = "llm-pi-ai"


def default_path() -> Path:
    """Return the credential file DSH itself uses: ``~/.dsh/.credentials.yaml``.

    Kept as a function (not a module constant) so tests can point the loader at
    a temporary file without ever touching the real one.
    """
    return Path.home() / ".dsh" / DEFAULT_FILENAME


# ---------------------------------------------------------------------------
# A YAML subset reader (plain block style only)
# ---------------------------------------------------------------------------
class _YamlSubsetError(ValueError):
    """Raised when a document is outside the YAML subset this module reads."""


_UNESCAPES = {
    "0": "\0",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "\\": "\\",
    '"': '"',
    "/": "/",
}


def _unescape_double(text: str) -> str:
    """Resolve backslash escapes of a double-quoted scalar."""
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            out.append(_UNESCAPES.get(text[index + 1], text[index + 1]))
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _unquote(text: str) -> str:
    """Strip one pair of surrounding quotes, honouring YAML's escapes."""
    candidate = text.strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "\"'":
        inner = candidate[1:-1]
        if candidate[0] == "'":
            return inner.replace("''", "'")
        return _unescape_double(inner)
    return candidate


def _strip_trailing_comment(text: str) -> str:
    """Drop a ``#`` comment, which YAML only starts after whitespace."""
    stripped = text.strip()
    if stripped.startswith("#"):
        return ""
    marker = text.find(" #")
    if marker >= 0:
        return text[:marker]
    return text


def _parse_scalar(text: str) -> Any:
    """Read an inline scalar: quoted string, ``null``/``~``, bool, or plain text."""
    candidate = text.strip()
    if not candidate:
        return None
    if candidate[0] in "\"'":
        return _unquote(candidate)
    candidate = _strip_trailing_comment(candidate).strip()
    if not candidate:
        return None
    lowered = candidate.lower()
    if lowered in ("null", "~"):
        return None
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    return candidate


def _split_entry(content: str) -> tuple[str, str] | None:
    """Split ``key: value`` at the first separator outside quotes.

    The separator only counts when followed by whitespace or end of line, so a
    plain key such as ``client-connection/browser-session`` and a value such as
    ``https://host:443`` are not misread. Returns ``None`` when the line is not
    a mapping entry.
    """
    quote: str | None = None
    index = 0
    length = len(content)
    while index < length:
        char = content[index]
        if quote is not None:
            if quote == '"' and char == "\\":
                index += 2
                continue
            if char == quote:
                if quote == "'" and index + 1 < length and content[index + 1] == "'":
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in "\"'":
            quote = char
            index += 1
            continue
        if char == ":" and (index + 1 == length or content[index + 1] in " \t"):
            key = content[:index].strip()
            if not key:
                return None
            return _unquote(key), content[index + 1 :].strip()
        index += 1
    return None


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    """Parse the block starting at ``lines[index]`` whose first line is at *indent*."""
    if lines[index][1].startswith("- "):
        return _parse_sequence(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_mapping(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    """Parse ``key: value`` pairs indented exactly *indent*."""
    result: dict[str, Any] = {}
    while index < len(lines):
        line_indent, content = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            # A deeper line with no owning key: skip rather than guess.
            index += 1
            continue
        if content.startswith("- "):
            break
        entry = _split_entry(content)
        if entry is None:
            index += 1
            continue
        key, tail = entry
        index += 1
        if tail.startswith("|") or tail.startswith(">"):
            # Block scalar: not written by DSH, but cheap to keep readable.
            block: list[str] = []
            while index < len(lines) and lines[index][0] > indent:
                block.append(lines[index][1])
                index += 1
            result[key] = "\n".join(block) if tail.startswith("|") else " ".join(block)
            continue
        if tail:
            result[key] = _parse_scalar(tail)
            continue
        if index < len(lines) and lines[index][0] > indent:
            value, index = _parse_block(lines, index, lines[index][0])
            result[key] = value
        else:
            result[key] = None
    return result, index


def _parse_sequence(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    """Parse ``- item`` entries indented exactly *indent*."""
    items: list[Any] = []
    while index < len(lines):
        line_indent, content = lines[index]
        if line_indent > indent:
            index += 1
            continue
        if line_indent < indent or not content.startswith("- "):
            break
        rest = content[2:].strip()
        index += 1
        if not rest:
            if index < len(lines) and lines[index][0] > indent:
                value, index = _parse_block(lines, index, lines[index][0])
                items.append(value)
            else:
                items.append(None)
            continue
        entry = _split_entry(rest) if rest[0] not in "\"'" else None
        if entry is not None:
            key, tail = entry
            item: dict[str, Any] = {key: _parse_scalar(tail) if tail else None}
            # Continuation lines of a ``- key: value`` item are its siblings.
            if index < len(lines) and lines[index][0] > indent:
                extra, index = _parse_mapping(lines, index, lines[index][0])
                if isinstance(extra, Mapping):
                    item.update(extra)
            items.append(item)
            continue
        items.append(_parse_scalar(rest))
    return items, index


def _parse_document(text: str) -> dict[str, Any]:
    """Parse *text* into a mapping; a non-mapping document yields ``{}``.

    Raises :class:`_YamlSubsetError` for tab indentation, which this reader
    refuses instead of guessing a column.
    """
    lines: list[tuple[int, str]] = []
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    for raw in normalised.split("\n"):
        if "\t" in raw:
            raise _YamlSubsetError("the credentials file uses tab indentation")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in ("---", "..."):
            continue
        lines.append((len(raw) - len(raw.lstrip(" ")), stripped))
    if not lines:
        return {}
    value, _ = _parse_block(lines, 0, lines[0][0])
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def _secret_text(value: Any) -> str | None:
    """Normalise one candidate secret to a trimmed string, or ``None``.

    Only scalars count: a mapping or list in a scalar slot is structure that
    happens to be truthy, and treating it as a key would send nonsense in an
    ``Authorization`` header.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (Mapping, list, tuple, set)):
        return None
    text = str(value).strip()
    return text or None


def _scalar_map(value: Any) -> dict[str, str]:
    """Keep the scalar-valued entries of a parsed mapping."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, str] = {}
    for key, raw in value.items():
        text = _secret_text(raw)
        if text is not None:
            result[str(key)] = text
    return result


def _record_map(value: Any) -> dict[str, Mapping]:
    """Keep the mapping-valued entries (one record per ``<plugin>/<route>``)."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Mapping] = {}
    for key, raw in value.items():
        if isinstance(raw, Mapping):
            result[str(key)] = raw
    return result


class Credentials:
    """A resolved view of DSH's credential store, safe to print.

    Built through :meth:`load`; the constructor is internal. The instance holds
    the secret-bearing ``refs`` and ``records`` privately, and presentation
    (``repr``/``str``) reports counts and the file path only.
    """

    __slots__ = ("_path", "_refs", "_records", "_loaded", "_error", "_environ")

    def __init__(
        self,
        *,
        path: Path,
        refs: Mapping[str, str],
        records: Mapping[str, Mapping],
        loaded: bool,
        error: str | None,
        environ: Mapping[str, Any],
    ) -> None:
        self._path = path
        self._refs = dict(refs)
        self._records = dict(records)
        self._loaded = loaded
        self._error = error
        self._environ = dict(environ)

    # -- construction -------------------------------------------------------
    @classmethod
    def load(cls, path: Any = None, environ: Mapping[str, Any] | None = None) -> "Credentials":
        """Read the credential store, tolerating absence and damage.

        *path* defaults to :func:`default_path`; *environ* defaults to
        ``os.environ`` (it is snapshotted, so resolution is deterministic for
        the life of the instance). A missing file, an unreadable file, invalid
        UTF-8, tab indentation, or a document that is not a mapping all yield an
        empty, usable instance with :attr:`error` describing the problem — never
        an exception, because a broken credential file must only ever mean "no
        key".
        """
        env = os.environ if environ is None else environ
        target = default_path() if path is None else Path(str(path)).expanduser()
        try:
            raw = target.read_bytes()
        except OSError as exc:
            return cls(
                path=target,
                refs={},
                records={},
                loaded=False,
                error=f"credentials file is unavailable ({type(exc).__name__})",
                environ=env,
            )
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return cls(
                path=target,
                refs={},
                records={},
                loaded=False,
                error="credentials file is not UTF-8 text",
                environ=env,
            )
        try:
            document = _parse_document(text)
        except _YamlSubsetError as exc:
            return cls(
                path=target,
                refs={},
                records={},
                loaded=False,
                error=str(exc),
                environ=env,
            )
        except Exception:  # pragma: no cover - defensive: never explode on YAML
            return cls(
                path=target,
                refs={},
                records={},
                loaded=False,
                error="credentials file could not be parsed",
                environ=env,
            )
        return cls(
            path=target,
            refs=_scalar_map(document.get("refs")),
            records=_record_map(document.get("records")),
            loaded=True,
            error=None,
            environ=env,
        )

    # -- inspection ---------------------------------------------------------
    @property
    def path(self) -> Path:
        """The file this instance was loaded from (whether or not it existed)."""
        return self._path

    @property
    def loaded(self) -> bool:
        """Whether the file was read and parsed."""
        return self._loaded

    @property
    def error(self) -> str | None:
        """Why the file could not be used, or ``None``. Contains no secrets."""
        return self._error

    @property
    def ref_names(self) -> tuple[str, ...]:
        """Names defined under ``refs`` (names are not secrets)."""
        return tuple(sorted(self._refs))

    @property
    def record_ids(self) -> tuple[str, ...]:
        """Record ids defined under ``records``, e.g. ``llm-pi-ai/iris``."""
        return tuple(sorted(self._records))

    # -- resolution ---------------------------------------------------------
    def resolve(self, api_key_env: str | None, route: str) -> str | None:
        """Resolve the API key for *route*, or ``None`` when there is none.

        Order, per the project contract:

        1. ``environ[api_key_env]``;
        2. ``refs[api_key_env]``;
        3. ``records["llm-pi-ai/<route>"].payload`` — ``apiKey``, ``key``,
           ``token``, ``secret`` in that order.

        A blank *api_key_env* means the route names no credential reference, so
        steps 1-2 do not apply and steps 3 still does not either: the route was
        not configured for credential lookup at all, and inventing a key for it
        would silently authenticate against a record the user never bound to
        this route.
        """
        name = (api_key_env or "").strip()
        if not name:
            return None

        from_env = _secret_text(self._env_lookup(name))
        if from_env is not None:
            return from_env

        from_refs = _secret_text(self._refs.get(name))
        if from_refs is not None:
            return from_refs

        return self._record_key(route)

    def _env_lookup(self, name: str) -> Any:
        try:
            return self._environ.get(name)
        except Exception:  # pragma: no cover - a hostile environ mapping
            return None

    def _record_key(self, route: str) -> str | None:
        name = (route or "").strip()
        if not name:
            return None
        record = self._records.get(f"{_RECORD_PLUGIN}/{name}")
        if not isinstance(record, Mapping):
            return None
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            return None
        lowered = {str(key).lower(): value for key, value in payload.items()}
        for candidate in _PAYLOAD_KEY_NAMES:
            text = _secret_text(lowered.get(candidate))
            if text is not None:
                return text
        return None

    # -- presentation -------------------------------------------------------
    def __repr__(self) -> str:
        return (
            f"Credentials(path={str(self._path)!r}, loaded={self._loaded}, "
            f"refs={len(self._refs)}, records={len(self._records)}, "
            f"error={self._error!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Credentials):
            return NotImplemented
        return (
            self._path == other._path
            and self._refs == other._refs
            and self._records == other._records
        )

    def __hash__(self) -> int:  # pragma: no cover - instances are not keys
        return hash((self._path, tuple(sorted(self._refs)), tuple(sorted(self._records))))
