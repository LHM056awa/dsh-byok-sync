"""HTTP transport for DSH third-party endpoint discovery and balances.

Two responsibilities live here:

* **Model discovery** — turn a provider's ``baseURL`` + ``api`` into the URL of
  its model-listing endpoint (:func:`normalize_models_url`), build the request
  headers for that protocol (:func:`build_auth_headers`), and read the reply
  into :class:`~dsh_byok_sync.models.FetchResult` (:func:`fetch_models`).
* **Balance display** — ask the handful of vendors whose balance endpoint we
  actually know (:func:`fetch_credits`, :func:`credits_endpoint`,
  :func:`known_credit_host`).

The wire rules mirror the DeepSeek Harness implementation
(``dsh-llm-pi-ai/src/discovery.ts``): OpenAI protocols list at
``{baseURL}/models`` with ``Authorization: Bearer``, Anthropic Messages at
``<root>/v1/models?limit=1000`` with ``x-api-key`` + ``anthropic-version``, and
Gemini at ``{baseURL}/models?pageSize=1000`` with ``x-goog-api-key``.

Everything here is *total*: no function raises because of a bad URL, a bad
status code, a timeout, or a malformed body. Failures come back as a
:class:`FetchResult` carrying a short, human-readable ``error`` (or as ``None``
for balances), because one dead endpoint must never abort a whole sync run.

Credentials never leak. This module receives an already-resolved API key, uses
it only to build a request header, and never logs, prints, or stores it; error
texts are additionally scrubbed of every header value before they are returned
(see :func:`_redact`), so a hostile or buggy :func:`opener` cannot smuggle a key
into a report.

The network call is injectable: an *opener* is any callable with the signature
``opener(request: urllib.request.Request, timeout: float) -> (status, bytes)``.
Tests pass a stand-in and never touch the network. The default implementation,
:func:`default_opener`, obeys exactly the same contract (it converts an
``http.client.HTTPResponse`` into ``(status, bytes)`` and reports HTTP error
statuses rather than raising them).
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from . import __version__
from .models import FetchResult, RemoteModel

__all__ = [
    "ANTHROPIC_MODEL_LIMIT",
    "ANTHROPIC_VERSION",
    "DEFAULT_TIMEOUT",
    "GEMINI_MODEL_LIMIT",
    "ListingError",
    "TransportError",
    "build_auth_headers",
    "credits_endpoint",
    "default_opener",
    "fetch_credits",
    "fetch_models",
    "known_credit_host",
    "normalize_models_url",
    "parse_models_payload",
]

#: Default request timeout in seconds; mirrors the CLI ``--timeout`` default.
DEFAULT_TIMEOUT = 15.0

#: Stable API version Anthropic's model-listing endpoint requires.
ANTHROPIC_VERSION = "2023-06-01"

#: Largest model-list page accepted by Anthropic's public listing endpoint.
ANTHROPIC_MODEL_LIMIT = 1000

#: Largest model-list page accepted by the Gemini API.
GEMINI_MODEL_LIMIT = 1000

#: Bodies larger than this are refused: a truncated model listing cannot be
#: parsed, so overflowing is reported instead of silently cut short.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

#: Protocol ids (as written in ``llm-pi-ai`` provider profiles).
API_OPENAI_COMPLETIONS = "openai-completions"
API_OPENAI_RESPONSES = "openai-responses"
API_ANTHROPIC_MESSAGES = "anthropic-messages"
API_GEMINI = "google-generative-ai"

#: Every protocol that shares the OpenAI ``{baseURL}/models`` + bearer shape.
_OPENAI_LIKE_APIS = frozenset({API_OPENAI_COMPLETIONS, API_OPENAI_RESPONSES})

#: Header each protocol authenticates with.
_AUTH_HEADER_NAMES: dict[str, str] = {
    API_OPENAI_COMPLETIONS: "Authorization",
    API_ANTHROPIC_MESSAGES: "x-api-key",
    API_GEMINI: "x-goog-api-key",
}

#: Trailing path segments that name a *different* operation than "list models".
#: Stripping them lets an over-specific ``baseURL`` still resolve; longest
#: suffixes first so ``/chat/completions`` wins over ``/completions``.
_OPERATION_SUFFIXES: tuple[str, ...] = (
    "/chat/completions",
    "/responses",
    "/completions",
    "/embeddings",
)

#: A usable header-carried key: printable ASCII, no whitespace at all.
_HEADER_SAFE_KEY = re.compile(r"[\x21-\x7e]+")

#: Marker substituted for scrubbed secrets.
_REDACTED = "<redacted>"

#: An injectable HTTP transport: ``(request, timeout) -> (status, bytes)``.
Opener = Callable[[urllib.request.Request, float], "tuple[int, bytes]"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class TransportError(Exception):
    """Raised by an opener when it cannot produce a status/body pair.

    The message is user-facing; it must not contain a credential (any header
    value that reaches :func:`fetch_models` is scrubbed anyway).
    """


class ListingError(Exception):
    """Raised while reading a listing body that has no recognisable shape."""


# ---------------------------------------------------------------------------
# Protocol + URL construction
# ---------------------------------------------------------------------------
def _normalize_api(api: str | None) -> str:
    """Map a profile's ``api`` onto the three wire shapes this module speaks.

    ``openai-responses`` lists its models exactly like ``openai-completions``,
    a missing value defaults to OpenAI Chat Completions (the shape a gateway is
    overwhelmingly likely to speak, as DSH's own discovery does), and an
    unknown protocol is treated the same way rather than refused: the URL then
    either answers with models or answers 404, which is more useful than a
    local error.
    """
    value = (api or "").strip().lower()
    if value == API_ANTHROPIC_MESSAGES:
        return API_ANTHROPIC_MESSAGES
    if value == API_GEMINI:
        return API_GEMINI
    if value in _OPENAI_LIKE_APIS:
        return API_OPENAI_COMPLETIONS
    return API_OPENAI_COMPLETIONS


def _protocol_query(protocol: str) -> str:
    """The query string the listing endpoint of *protocol* needs, if any."""
    if protocol == API_GEMINI:
        return f"pageSize={GEMINI_MODEL_LIMIT}"
    if protocol == API_ANTHROPIC_MESSAGES:
        return f"limit={ANTHROPIC_MODEL_LIMIT}"
    return ""


def _compose(scheme: str, netloc: str, path: str, query: str) -> str:
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_models_url(base_url: str, api: str) -> str:
    """Map a provider ``baseURL`` + ``api`` onto its model-listing URL.

    The result is always an absolute ``http(s)`` URL, or ``""`` when
    *base_url* cannot be turned into one (empty, whitespace only, or missing a
    scheme/host). Callers treat ``""`` as "no endpoint" and report it; nothing
    here raises, because a bad base URL in one route must not abort the run.

    The rules, in order — they exist because gateway documentation publishes
    half a dozen spellings of the same base:

    1. Surrounding whitespace, the query string, and the fragment are dropped:
       the listing is not parameterised by them, and DSH's own discovery throws
       them away too. A caller-supplied ``?page=2`` therefore does **not**
       survive; this tool reads one page, like DSH.
    2. A trailing path segment naming another operation is removed
       (case-insensitive): ``/chat/completions``, ``/responses``,
       ``/completions``, ``/embeddings``. So
       ``https://host/v1/chat/completions`` and ``https://host/v1`` are the same
       base.
    3. A trailing ``/models`` is removed as well, so the listing path can be
       appended exactly once. When the base *is* the listing (``https://host/models``)
       it is kept as written — a bare host with no API version must not have
       ``/v1`` invented in front of a path the operator spelled out.
    4. Trailing slashes are dropped. A base of ``/`` or nothing at all is an
       API root, so ``/v1`` *is* added there: ``https://api.example.com`` →
       ``https://api.example.com/v1/models``. Any other path is treated as a
       deployment prefix and left alone — ``https://gw.example/openai`` →
       ``https://gw.example/openai/models``, never ``.../openai/v1/models``,
       because the prefix may be exactly what the gateway routes on (DSH treats
       ``baseURL`` as a prefix for the same reason). An explicit version segment
       such as ``/v2`` or ``/v1beta`` therefore survives untouched.
    5. Protocol-specific shapes (decided before rule 4, so even an empty base
       gets its protocol's shape): Gemini appends ``/models?pageSize=1000`` to
       the base unchanged (its ``baseURL`` already carries the API version);
       Anthropic strips one trailing ``/v1`` and appends ``/v1/models?limit=1000``
       so both published spellings of its root land on the same endpoint.

    Every step is idempotent: feeding a listing URL back in returns it unchanged,
    for all three protocols.

    Case is preserved in the surviving prefix except for the comparisons in
    rules 2-3, which are case-insensitive.
    """
    cleaned = (base_url or "").strip()
    if not cleaned:
        return ""
    parts = urlsplit(cleaned)
    if not parts.scheme or not parts.netloc:
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""

    protocol = _normalize_api(api)
    path = parts.path.rstrip("/")

    # Rule 2: drop a trailing operation segment naming something else.
    for suffix in _OPERATION_SUFFIXES:
        if path.lower().endswith(suffix):
            path = path[: len(path) - len(suffix)]
            break
    path = path.rstrip("/")

    # Rule 3: drop a trailing /models so it is appended exactly once.
    if path.lower().endswith("/models"):
        remainder = path[: -len("/models")].rstrip("/")
        if not remainder:
            # The base URL *is* the listing endpoint; honour it verbatim.
            return _compose(parts.scheme, parts.netloc, path, _protocol_query(protocol))
        path = remainder

    # Rule 5: protocol-specific assembly.
    if protocol == API_GEMINI:
        return _compose(parts.scheme, parts.netloc, f"{path}/models", _protocol_query(protocol))
    if protocol == API_ANTHROPIC_MESSAGES:
        root = path[: -len("/v1")] if path.lower().endswith("/v1") else path
        return _compose(
            parts.scheme, parts.netloc, f"{root}/v1/models", _protocol_query(protocol)
        )

    # Rule 4: an empty path is an API root, so /v1 is added; any other prefix is
    # left as the operator wrote it.
    if not path:
        return _compose(parts.scheme, parts.netloc, "/v1/models", "")
    return _compose(parts.scheme, parts.netloc, f"{path}/models", "")


def _has_header(headers: Mapping[str, Any], name: str) -> bool:
    """Whether *headers* carries *name* (case-insensitive) with a non-empty value."""
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered and str(value).strip():
            return True
    return False


def _usable_key(api_key: Any) -> str | None:
    """Return *api_key* trimmed when it can legally travel in an HTTP header.

    Blank keys mean "no key" (the request degrades to unauthenticated instead of
    failing). A key holding whitespace, control characters, or non-ASCII bytes
    cannot be encoded into a header; it is refused here, before any request, so
    the failure surfaces as the endpoint's own 401 instead of a local
    ``UnicodeEncodeError``. The value is never echoed anywhere.
    """
    if api_key is None:
        return None
    text = str(api_key).strip()
    if not text:
        return None
    if _HEADER_SAFE_KEY.fullmatch(text) is None:
        return None
    return text


def build_auth_headers(
    api: str, api_key: str | None, headers: dict[str, str]
) -> dict[str, str]:
    """Return the complete request-header dict for one listing request.

    *headers* is the provider's own configured header map (``HTTP-Referer``,
    a deployment-owned ``Authorization``, ...). It is never mutated: a fresh
    dict is returned, so the caller's configuration stays byte-identical.

    The merge order is deliberate and keeps explicit configuration in charge:

    * every configured header is copied through untouched;
    * ``Accept: application/json`` is added when the route did not set one;
    * Anthropic always gets ``anthropic-version: 2023-06-01`` (its listing
      endpoint refuses requests without it) unless the route pinned its own;
    * the protocol's auth header is added **only** when a usable key exists and
      the route did not already set that header, so a route that authenticates
      through its own ``Authorization`` is never overridden by a stray key. With
      no key at all the request goes out unauthenticated — a 401 from the
      endpoint is a better diagnosis than an invented header.
    """
    protocol = _normalize_api(api)
    merged: dict[str, str] = {}
    for key, value in dict(headers or {}).items():
        merged[str(key)] = str(value)

    if not _has_header(merged, "Accept"):
        merged["Accept"] = "application/json"

    if protocol == API_ANTHROPIC_MESSAGES and not _has_header(merged, "anthropic-version"):
        merged["anthropic-version"] = ANTHROPIC_VERSION

    usable = _usable_key(api_key)
    if usable is None:
        return merged

    auth_name = _AUTH_HEADER_NAMES[protocol]
    if _has_header(merged, auth_name):
        return merged
    if protocol == API_OPENAI_COMPLETIONS:
        merged[auth_name] = f"Bearer {usable}"
    else:
        merged[auth_name] = usable
    return merged


# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------
def _redact(message: Any, headers: Mapping[str, Any] | None) -> str:
    """Collapse *message* into one line with every header value scrubbed out.

    Two defences in one: whitespace is flattened (so a multi-line exception
    cannot break the report), and every header value — plus each whitespace-
    separated long token inside it, which is how ``Bearer sk-...`` becomes
    scrubbable — is replaced by ``<redacted>``. Whatever an opener raises, the
    resulting text can be shown to the user.
    """
    text = " ".join(str(message or "").split())
    if not text:
        text = "request failed"
    for value in (headers or {}).values():
        raw = str(value)
        if len(raw) >= 4:
            text = text.replace(raw, _REDACTED)
        for token in raw.split():
            if len(token) >= 8:
                text = text.replace(token, _REDACTED)
    if len(text) > 300:
        text = text[:300] + "..."
    return text


# ---------------------------------------------------------------------------
# Default opener
# ---------------------------------------------------------------------------
def _status_of(response: Any) -> int:
    """Best-effort HTTP status of an ``http.client.HTTPResponse``."""
    for attribute in ("status", "code"):
        value = getattr(response, attribute, None)
        if isinstance(value, int):
            return value
    getcode = getattr(response, "getcode", None)
    if callable(getcode):
        value = getcode()
        if isinstance(value, int):
            return value
    return 200


def default_opener(request: urllib.request.Request, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, bytes]:
    """Perform *request* and return ``(status, body)``.

    This is the production :data:`Opener`; it obeys the same ``(status, bytes)``
    contract the tests inject, so no caller ever sees an ``HTTPResponse``.

    HTTP error statuses are returned, not raised: ``urllib`` models them as
    ``HTTPError`` (which is also a response), and reporting ``403`` lets the
    caller render one uniform message shape. Genuine transport faults — DNS,
    refused connections, TLS, timeouts, oversize bodies — raise
    :class:`TransportError` with a scrubbable message. The API key travels only
    inside the request headers and is never part of any message here.
    """
    if not _has_header(dict(request.header_items()), "User-Agent"):
        request.add_header("User-Agent", f"dsh-byok-sync/{__version__}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = _status_of(response)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise TransportError(f"response exceeded {MAX_RESPONSE_BYTES} bytes")
        return status, raw
    except urllib.error.HTTPError as exc:
        # Drain and discard: the body of an error reply is never echoed, because
        # it is server-controlled text that may quote the request back.
        try:
            exc.read()
        except Exception:  # pragma: no cover - draining is best effort
            pass
        return int(getattr(exc, "code", 0) or 0), b""
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise TransportError("request timed out") from exc
        if isinstance(reason, ssl.SSLError):
            raise TransportError(f"TLS error: {reason}") from exc
        detail = "request timed out" if reason is None else f"could not reach the endpoint ({reason})"
        raise TransportError(detail) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise TransportError("request timed out") from exc
    except OSError as exc:  # socket errors surface here too
        raise TransportError(f"network error ({exc})") from exc


# ---------------------------------------------------------------------------
# Listing parsing
# ---------------------------------------------------------------------------
def _decode_body(body: Any) -> str:
    """Decode what an opener returned into text (``bytes``, ``str``, or None)."""
    if body is None:
        return ""
    if isinstance(body, (bytes, bytearray, memoryview)):
        return bytes(body).decode("utf-8", errors="replace")
    return str(body)


def _first_text(*candidates: Any) -> str | None:
    """First non-empty stripped string among *candidates*."""
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _clean_resource_id(value: Any) -> str | None:
    """Normalise a listing id: trim it and drop Gemini's ``models/`` prefix."""
    text = _first_text(value)
    if text is None:
        return None
    if text.startswith("models/"):
        text = text[len("models/") :].strip()
    return text or None


def _capacity(*candidates: Any) -> int | None:
    """First candidate usable as a positive token capacity.

    DSH only accepts positive integers here, and so do we, because ``0`` would
    silently disable a model. Strings of digits are accepted too: gateways
    routinely serialise capacities as JSON strings, and refusing them would drop
    reliable information. Booleans are rejected explicitly (``True`` is an
    ``int`` in Python) and everything else falls through to the next candidate.
    """
    for candidate in candidates:
        if isinstance(candidate, bool):
            continue
        if isinstance(candidate, int):
            if candidate > 0:
                return candidate
            continue
        if isinstance(candidate, float):
            if candidate > 0 and candidate.is_integer():
                return int(candidate)
            continue
        if isinstance(candidate, str):
            text = candidate.strip()
            if text.isdigit() and int(text) > 0:
                return int(text)
    return None


def _nested(entry: Mapping[str, Any], *path: str) -> Any:
    """Read a dotted path out of a listing entry, tolerating absent/wrong types."""
    current: Any = entry
    for segment in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(segment)
    return current


def _entry_name(raw: Mapping[str, Any]) -> str | None:
    """Best display name for a listing entry.

    The union DSH reads is ``name`` / ``display_name`` / ``displayName``, in that
    order — with one exception: Gemini reports ``name`` as a *resource* name
    (``models/gemini-3-pro``), which is an id, not a label. A resource-style
    ``name`` is therefore demoted below ``displayName`` and, when neither
    resolves, used de-prefixed (which simply equals the id).
    """
    raw_name = _first_text(raw.get("name"))
    if raw_name is not None and not raw_name.startswith("models/"):
        return raw_name
    preferred = _first_text(raw.get("display_name"), raw.get("displayName"))
    if preferred is not None:
        return preferred
    if raw_name is not None:
        return raw_name[len("models/") :].strip() or None
    return None


def _model_from_entry(key: str | None, raw: Any) -> RemoteModel | None:
    """Read one listing entry; ``None`` when it carries no usable id.

    Field names follow exactly the union DSH reads (``context_window``,
    ``max_input_tokens``, ``limit.context``, ``top_provider.max_completion_tokens``,
    Gemini's ``inputTokenLimit``/``outputTokenLimit``, ...). A single malformed
    row is skipped instead of failing the whole endpoint: one bad entry must not
    hide a working catalog. Gemini entries that declare generation methods
    without ``generateContent`` (embedding models, for instance) cannot serve a
    conversation and are dropped here; DSH does the same.
    """
    if isinstance(raw, str):
        model_id = _clean_resource_id(key or raw)
        return RemoteModel(id=model_id) if model_id else None
    if not isinstance(raw, Mapping):
        return None

    methods = raw.get("supportedGenerationMethods")
    if isinstance(methods, Sequence) and not isinstance(methods, str) and methods:
        if "generateContent" not in methods:
            return None

    model_id = _clean_resource_id(key) if isinstance(key, str) and key.strip() else None
    if model_id is None:
        # Lenient fallback: some gateways only ship a resource name.
        model_id = _clean_resource_id(raw.get("id")) or _clean_resource_id(raw.get("name"))
    if model_id is None:
        return None

    name = _entry_name(raw)

    context_window = _capacity(
        raw.get("contextWindow"),
        raw.get("context_window"),
        raw.get("context_length"),
        raw.get("max_input_tokens"),
        _nested(raw, "limit", "context"),
        raw.get("inputTokenLimit"),
    )
    max_tokens = _capacity(
        raw.get("maxOutputTokens"),
        raw.get("max_output_tokens"),
        raw.get("maxTokens"),
        raw.get("max_tokens"),
        _nested(raw, "limit", "output"),
        _nested(raw, "top_provider", "max_completion_tokens"),
        raw.get("outputTokenLimit"),
    )
    return RemoteModel(
        id=model_id, name=name, contextWindow=context_window, maxTokens=max_tokens
    )


def parse_models_payload(payload: Any) -> tuple[RemoteModel, ...]:
    """Read a model-listing JSON payload into :class:`RemoteModel` entries.

    One reader serves all three protocols because the shapes overlap:

    * a ``data`` array (OpenAI, and most gateways) wins when present — even when
      empty — because DSH prefers it too;
    * a ``models`` **map** uses each property key as the endpoint-facing id (the
      nested ``id`` is only a fallback), which is what enriched gateway
      directories ship;
    * a ``models`` **array** is Gemini's shape: ids come from resource names with
      the ``models/`` prefix stripped, and capacities from
      ``inputTokenLimit``/``outputTokenLimit``.

    Raises :class:`ListingError` when neither supported key holds an entry
    collection: iterating a dict's keys or a string's characters there would
    fabricate garbage ids, and a fabricated catalog under default delete rules
    could destroy the local model list. An empty but well-formed listing is a
    success with zero models — the caller decides what that means for deletions.
    """
    rows: list[tuple[str | None, Any]] = []
    if isinstance(payload, Mapping):
        data = payload.get("data")
        models = payload.get("models")
        if isinstance(data, list):
            rows = [(None, item) for item in data]
        elif isinstance(data, Mapping):
            rows = [(None, data)]
        elif isinstance(models, Mapping):
            rows = [(str(key), item) for key, item in models.items()]
        elif isinstance(models, list):
            rows = [(None, item) for item in models]
        elif data is None and models is None:
            raise ListingError(
                'the endpoint\'s model listing has neither a "data" array nor a "models" object'
            )
        else:
            raise ListingError(
                'the endpoint\'s "data"/"models" field is not a list of model entries'
            )
    elif isinstance(payload, list):
        rows = [(None, item) for item in payload]
    else:
        raise ListingError("unexpected JSON payload type; expected an object or a list")

    models_out: list[RemoteModel] = []
    for key, raw in rows:
        model = _model_from_entry(key, raw)
        if model is not None:
            models_out.append(model)
    return tuple(models_out)


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------
def _http_error_text(status: int) -> str:
    """Short, actionable description of a non-2xx status (never a body echo)."""
    text = f"HTTP {status}"
    if status in (401, 403):
        return f"{text}; check the API key"
    if status == 404:
        return f"{text}; check the base URL — the endpoint has no model listing here"
    if status == 429:
        return f"{text}; rate limited, retry later"
    if 500 <= status <= 599:
        return f"{text}; the provider reported a server error"
    return text


def _unpack_opener_result(result: Any) -> tuple[int, Any]:
    """Validate an opener's ``(status, bytes)`` reply, tolerating loose shapes."""
    if isinstance(result, tuple) and len(result) == 2:
        status, body = result
    elif isinstance(result, list) and len(result) == 2:
        status, body = result
    else:
        raise TransportError("the opener did not return (status, body)")
    try:
        return int(status), body
    except (TypeError, ValueError) as exc:
        raise TransportError("the opener returned a non-numeric status") from exc


def fetch_models(
    url: str,
    headers: dict[str, str],
    timeout: float,
    *,
    opener: Opener | None = None,
) -> FetchResult:
    """GET *url* and read its model listing into a :class:`FetchResult`.

    *headers* is the **complete** header map to send — normally the output of
    :func:`build_auth_headers` — and is used verbatim; this function never adds
    or rewrites an auth header of its own.

    Failures never raise. A blank URL, a non-2xx status, a timeout, a refused
    connection, an unparseable body, or an unrecognisable listing each come back
    as ``FetchResult(url=..., models=(), error="...")`` where the message is
    short, single-line, and scrubbed of every header value, so a report can
    print it as-is. ``FetchResult.url`` always carries the URL that was actually
    requested, which is what the CLI shows next to the error.
    """
    target = (url or "").strip()
    if not target:
        return FetchResult(url="", models=(), error="no endpoint url")

    request_headers = {str(key): str(value) for key, value in dict(headers or {}).items()}
    call: Opener = opener if opener is not None else default_opener

    # Building the request is part of the fallible path: a header name or value
    # ``urllib`` refuses must surface as a report line, not as an exception.
    try:
        request = urllib.request.Request(target, method="GET", headers=request_headers)
        result = call(request, timeout)
    except TransportError as exc:
        return FetchResult(url=target, models=(), error=_redact(str(exc), request_headers))
    except Exception as exc:  # defensive: one endpoint must not abort the run
        detail = str(exc) or type(exc).__name__
        return FetchResult(
            url=target, models=(), error="request failed: " + _redact(detail, request_headers)
        )

    try:
        status, body = _unpack_opener_result(result)
    except TransportError as exc:
        return FetchResult(url=target, models=(), error=str(exc))

    if not 200 <= status <= 299:
        return FetchResult(
            url=target, models=(), error=_http_error_text(status), meta={"status": status}
        )

    text = _decode_body(body)
    if not text.strip():
        return FetchResult(
            url=target, models=(), error="the endpoint returned an empty body", meta={"status": status}
        )
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return FetchResult(
            url=target,
            models=(),
            error="the endpoint did not answer with JSON",
            meta={"status": status},
        )

    try:
        models = parse_models_payload(payload)
    except ListingError as exc:
        return FetchResult(url=target, models=(), error=str(exc), meta={"status": status})

    return FetchResult(url=target, models=models, error=None, meta={"status": status})


# ---------------------------------------------------------------------------
# Balances
# ---------------------------------------------------------------------------
#: Balance endpoints we actually know, keyed by the *host* of a provider's base
#: URL. Only these are ever queried: guessing a path for any other vendor would
#: report a 404 as a broken balance and clutter every report. Each entry is
#: ``host -> {url, currency, fields, currency_path}`` where ``fields`` is a list
#: of ``(json_path, scale)`` whose scaled values are summed, so a vendor that
#: reports a grant and a consumption as two numbers yields the remainder.
_CREDITS_ENDPOINTS: dict[str, dict[str, Any]] = {
    "api.deepseek.com": {
        "url": "https://api.deepseek.com/user/balance",
        "currency": "CNY",
        "fields": [("balance_infos[0].total_balance", 1.0)],
        "currency_path": "balance_infos[0].currency",
    },
    "openrouter.ai": {
        "url": "https://openrouter.ai/api/v1/credits",
        "currency": "USD",
        # Purchased credits minus usage: the number an operator cares about is
        # what is left, not what was ever bought.
        "fields": [("data.total_credits", 1.0), ("data.total_usage", -1.0)],
        "currency_path": None,
    },
    "api.moonshot.cn": {
        "url": "https://api.moonshot.cn/v1/users/me/balance",
        "currency": "CNY",
        "fields": [("data.available_balance", 1.0)],
        "currency_path": None,
    },
    "api.moonshot.ai": {
        "url": "https://api.moonshot.ai/v1/users/me/balance",
        "currency": "USD",
        "fields": [("data.available_balance", 1.0)],
        "currency_path": None,
    },
    "api.stepfun.com": {
        "url": "https://api.stepfun.com/v1/accounts",
        "currency": "CNY",
        "fields": [("balance", 1.0)],
        "currency_path": None,
    },
    "api.novita.ai": {
        "url": "https://api.novita.ai/v3/user/balance",
        "currency": "USD",
        # Novita reports 1/10000 USD units.
        "fields": [("availableBalance", 0.0001)],
        "currency_path": None,
    },
}

#: A currency code we are willing to print.
_CURRENCY_CODE = re.compile(r"[A-Z]{3}")

#: A dotted JSON path segment, optionally with ``[n]`` index parts.
_INDEX_PART = re.compile(r"\[([0-9]+)\]")


def _host_of(url: str) -> str | None:
    """Lower-cased hostname of *url* (port, userinfo, and path dropped)."""
    text = (url or "").strip()
    if not text:
        return None
    parts = urlsplit(text)
    host = parts.hostname
    if host is None:
        # Tolerate a bare host, e.g. "api.deepseek.com/v1".
        host = urlsplit(f"//{text}").hostname
    return host.lower() if host else None


def _credits_spec(base_url: str) -> dict[str, Any] | None:
    """Balance spec for *base_url*'s host, or ``None`` when the vendor is unknown."""
    host = _host_of(base_url)
    if host is None:
        return None
    return _CREDITS_ENDPOINTS.get(host)


def known_credit_host(url: str) -> str | None:
    """Return the known balance host *url* belongs to, or ``None``.

    The CLI uses this to decide whether a ``credits:`` line is printed at all: a
    known host whose query fails is reported as ``unavailable``, while an
    unknown vendor gets no line (silence, not a bogus "unavailable"). This
    consults exactly the table :func:`fetch_credits` uses, so the two can never
    disagree.
    """
    host = _host_of(url)
    if host is None:
        return None
    return host if host in _CREDITS_ENDPOINTS else None


def credits_endpoint(base_url: str) -> tuple[str, str] | None:
    """Return ``(balance_url, currency)`` for *base_url*'s vendor, else ``None``.

    ``None`` means "this vendor has no balance endpoint we know" — it is not an
    error, and no request is made.
    """
    spec = _credits_spec(base_url)
    if spec is None:
        return None
    return spec["url"], spec["currency"]


def _json_path(payload: Any, path: str) -> tuple[bool, Any]:
    """Resolve a dotted path such as ``balance_infos[0].total_balance``.

    Returns ``(found, value)`` so a legitimate ``0``/``False`` is distinguishable
    from a missing key. A malformed spec path (empty brackets, non-numeric or
    signed indexes) is rejected rather than silently ignored, because a mistyped
    path could otherwise resolve to the wrong field.
    """
    current: Any = payload
    for segment in path.split("."):
        brackets = _INDEX_PART.findall(segment)
        key = _INDEX_PART.sub("", segment).strip()
        if key:
            if not isinstance(current, Mapping) or key not in current:
                return False, None
            current = current[key]
        for raw_index in brackets:
            index = int(raw_index)
            if not isinstance(current, list) or not 0 <= index < len(current):
                return False, None
            current = current[index]
    return True, current


def _number(value: Any) -> float | None:
    """Coerce a JSON scalar to ``float`` (vendors report numbers as strings)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def fetch_credits(
    base_url: str,
    api_key: str | None,
    headers: dict[str, str],
    timeout: float,
    *,
    opener: Opener | None = None,
) -> str | None:
    """Fetch a vendor's balance as a short display string, or ``None``.

    Only hosts in :data:`_CREDITS_ENDPOINTS` are queried; every other vendor
    returns ``None`` **without any network call**, which is what keeps a sync run
    from spraying guesses at endpoints that do not exist. For a known host, any
    failure (transport fault, non-2xx, non-JSON, missing field) also yields
    ``None``: a balance is decoration and must never disturb model syncing.

    The result looks like ``"CNY 12.34"`` or ``"USD 3.50"``. Two details worth
    knowing:

    * the currency is the vendor's configured one, except that DeepSeek's reply
      names its own currency and that wins when it is a plain three-letter code
      (an account billed in USD should not be labelled CNY);
    * when a vendor reports a grant and its consumption separately (OpenRouter),
      the printed value is what remains — and it is shown only when the credited
      amount itself resolved, because a usage figure on its own would render as
      a nonsensical negative remainder.
    """
    spec = _credits_spec(base_url)
    if spec is None:
        return None

    request_headers = build_auth_headers(API_OPENAI_COMPLETIONS, api_key, headers)
    call: Opener = opener if opener is not None else default_opener
    try:
        request = urllib.request.Request(spec["url"], method="GET", headers=request_headers)
        status, body = _unpack_opener_result(call(request, timeout))
    except Exception:  # balances are best effort by definition
        return None
    if not 200 <= status <= 299:
        return None

    text = _decode_body(body).strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None

    fields: Iterable[tuple[str, float]] = spec.get("fields", ())
    total: float | None = None
    primary_found = False
    for position, (json_path, scale) in enumerate(fields):
        found, value = _json_path(payload, json_path)
        if not found:
            continue
        number = _number(value)
        if number is None:
            continue
        total = number * scale if total is None else total + number * scale
        if position == 0:
            primary_found = True
    if total is None or not primary_found:
        return None

    currency = str(spec["currency"])
    currency_path = spec.get("currency_path")
    if currency_path:
        found, value = _json_path(payload, currency_path)
        if found and isinstance(value, str):
            candidate = value.strip().upper()
            if _CURRENCY_CODE.fullmatch(candidate):
                currency = candidate
    return f"{currency} {total:.2f}"
