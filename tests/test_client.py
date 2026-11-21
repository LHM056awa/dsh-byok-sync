"""Tests for :mod:`dsh_byok_sync.client`.

Every HTTP interaction goes through an injected opener; this module never opens
a socket, never reads the real credential file, and never prints a key.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

try:  # ``python -m unittest discover -s tests`` puts tests/ on sys.path
    import _helpers  # noqa: F401  (also puts ``src`` on sys.path)
except ModuleNotFoundError:  # ``python -m unittest tests.test_client`` does not
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _helpers  # noqa: F401

from dsh_byok_sync import client
from dsh_byok_sync.client import (
    ANTHROPIC_VERSION,
    build_auth_headers,
    credits_endpoint,
    fetch_credits,
    fetch_models,
    known_credit_host,
    normalize_models_url,
)
from dsh_byok_sync.models import RemoteModel

OPENAI = "openai-completions"
RESPONSES = "openai-responses"
ANTHROPIC = "anthropic-messages"
GEMINI = "google-generative-ai"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class RecordingOpener:
    """A stand-in opener that records calls and replays a canned reply."""

    def __init__(self, status: int = 200, body: Any = b"", error: BaseException | None = None):
        self.status = status
        self.body = body
        self.error = error
        self.calls: list[tuple[urllib.request.Request, float]] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> tuple[int, Any]:
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.status, self.body

    @property
    def last_request(self) -> urllib.request.Request:
        return self.calls[-1][0]

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FakeResponse:
    """Minimal ``http.client.HTTPResponse`` stand-in for the default opener."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body
        self.headers: dict[str, str] = {}

    def read(self, amount: int | None = None) -> bytes:
        return self._body if amount is None else self._body[:amount]

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        return False


def json_body(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


def header_value(request: urllib.request.Request, name: str) -> str | None:
    """Case-insensitive request-header lookup (``urllib`` re-cases names)."""
    for key, value in request.header_items():
        if key.lower() == name.lower():
            return value
    return None


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------
class NormalizeModelsUrlTests(unittest.TestCase):
    def test_api_root_gains_v1_models(self) -> None:
        self.assertEqual(
            normalize_models_url("https://api.example.com", OPENAI),
            "https://api.example.com/v1/models",
        )
        self.assertEqual(
            normalize_models_url("https://api.example.com/", OPENAI),
            "https://api.example.com/v1/models",
        )

    def test_v1_suffix_gains_models(self) -> None:
        for base_url in ("https://api.example.com/v1", "https://api.example.com/v1/"):
            with self.subTest(base_url=base_url):
                self.assertEqual(
                    normalize_models_url(base_url, OPENAI),
                    "https://api.example.com/v1/models",
                )

    def test_trailing_slashes_are_collapsed(self) -> None:
        self.assertEqual(
            normalize_models_url("https://api.example.com/v1///", OPENAI),
            "https://api.example.com/v1/models",
        )

    def test_chat_completions_suffix_is_replaced(self) -> None:
        for base_url in (
            "https://api.example.com/v1/chat/completions",
            "https://api.example.com/v1/Chat/Completions/",
        ):
            with self.subTest(base_url=base_url):
                self.assertEqual(
                    normalize_models_url(base_url, OPENAI),
                    "https://api.example.com/v1/models",
                )

    def test_responses_suffix_is_replaced(self) -> None:
        self.assertEqual(
            normalize_models_url("https://api.example.com/v1/responses", RESPONSES),
            "https://api.example.com/v1/models",
        )

    def test_completions_suffix_is_replaced(self) -> None:
        self.assertEqual(
            normalize_models_url("https://api.example.com/v1/completions", OPENAI),
            "https://api.example.com/v1/models",
        )
        self.assertEqual(
            normalize_models_url("https://api.example.com/chat/completions", OPENAI),
            "https://api.example.com/v1/models",
        )

    def test_query_and_fragment_are_dropped(self) -> None:
        for base_url in (
            "https://api.example.com/v1?token=abc",
            "https://api.example.com/v1#frag",
            "https://api.example.com/v1?api-version=1#frag",
        ):
            with self.subTest(base_url=base_url):
                self.assertEqual(
                    normalize_models_url(base_url, OPENAI),
                    "https://api.example.com/v1/models",
                )

    def test_already_a_listing_url_is_unchanged(self) -> None:
        self.assertEqual(
            normalize_models_url("https://api.example.com/v1/models", OPENAI),
            "https://api.example.com/v1/models",
        )
        self.assertEqual(
            normalize_models_url("https://api.example.com/v1/models/", OPENAI),
            "https://api.example.com/v1/models",
        )
        self.assertEqual(
            normalize_models_url("https://api.example.com/models", OPENAI),
            "https://api.example.com/models",
        )

    def test_deployment_prefix_is_left_alone(self) -> None:
        self.assertEqual(
            normalize_models_url("https://gw.example/openai/v1", OPENAI),
            "https://gw.example/openai/v1/models",
        )
        self.assertEqual(
            normalize_models_url("https://gw.example/openai", OPENAI),
            "https://gw.example/openai/models",
        )
        self.assertEqual(
            normalize_models_url("https://api.example.com/api/v3", OPENAI),
            "https://api.example.com/api/v3/models",
        )

    def test_openai_responses_lists_like_openai_completions(self) -> None:
        self.assertEqual(
            normalize_models_url("https://api.example.com/v1", RESPONSES),
            normalize_models_url("https://api.example.com/v1", OPENAI),
        )

    def test_missing_or_unknown_api_defaults_to_openai_shape(self) -> None:
        expected = "https://api.example.com/v1/models"
        self.assertEqual(normalize_models_url("https://api.example.com", ""), expected)
        self.assertEqual(normalize_models_url("https://api.example.com", None), expected)
        self.assertEqual(
            normalize_models_url("https://api.example.com", "azure-openai"), expected
        )

    def test_unusable_base_urls_are_empty(self) -> None:
        for base_url in ("", "   ", "not a url", "api.example.com/v1", "ftp://host/v1"):
            with self.subTest(base_url=base_url):
                self.assertEqual(normalize_models_url(base_url, OPENAI), "")

    def test_anthropic_always_lists_at_root_v1_models(self) -> None:
        expected = "https://api.anthropic.com/v1/models?limit=1000"
        for base_url in (
            "https://api.anthropic.com",
            "https://api.anthropic.com/",
            "https://api.anthropic.com/v1",
            "https://api.anthropic.com/v1/",
        ):
            with self.subTest(base_url=base_url):
                self.assertEqual(normalize_models_url(base_url, ANTHROPIC), expected)
        self.assertEqual(
            normalize_models_url("https://gw.example/anthropic/v1", ANTHROPIC),
            "https://gw.example/anthropic/v1/models?limit=1000",
        )

    def test_normalisation_is_idempotent_for_every_protocol(self) -> None:
        cases = (
            ("https://api.example.com", OPENAI),
            ("https://api.example.com/v1/chat/completions", OPENAI),
            ("https://gw.example/openai", OPENAI),
            ("https://api.anthropic.com/v1", ANTHROPIC),
            ("https://generativelanguage.googleapis.com/v1beta", GEMINI),
        )
        for base_url, api in cases:
            with self.subTest(base_url=base_url, api=api):
                once = normalize_models_url(base_url, api)
                self.assertEqual(normalize_models_url(once, api), once)

    def test_gemini_appends_page_size_and_keeps_the_version(self) -> None:
        self.assertEqual(
            normalize_models_url("https://generativelanguage.googleapis.com/v1beta", GEMINI),
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
        )
        self.assertEqual(
            normalize_models_url("https://g.example/", GEMINI),
            "https://g.example/models?pageSize=1000",
        )


# ---------------------------------------------------------------------------
# Header construction
# ---------------------------------------------------------------------------
class BuildAuthHeadersTests(unittest.TestCase):
    def test_openai_uses_bearer(self) -> None:
        headers = build_auth_headers(OPENAI, "sk-test", {})
        self.assertEqual(headers["Authorization"], "Bearer sk-test")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertNotIn("x-api-key", headers)

    def test_responses_protocol_uses_bearer(self) -> None:
        headers = build_auth_headers(RESPONSES, "sk-test", {})
        self.assertEqual(headers["Authorization"], "Bearer sk-test")

    def test_anthropic_uses_x_api_key_and_version(self) -> None:
        headers = build_auth_headers(ANTHROPIC, "sk-ant", {})
        self.assertEqual(headers["x-api-key"], "sk-ant")
        self.assertEqual(headers["anthropic-version"], ANTHROPIC_VERSION)
        self.assertNotIn("Authorization", headers)

    def test_anthropic_sends_version_even_without_key(self) -> None:
        headers = build_auth_headers(ANTHROPIC, None, {})
        self.assertEqual(headers["anthropic-version"], ANTHROPIC_VERSION)
        self.assertNotIn("x-api-key", headers)

    def test_gemini_uses_x_goog_api_key(self) -> None:
        headers = build_auth_headers(GEMINI, "AIza-test", {})
        self.assertEqual(headers["x-goog-api-key"], "AIza-test")
        self.assertNotIn("Authorization", headers)

    def test_no_key_means_no_auth_header(self) -> None:
        for value in (None, "", "   "):
            with self.subTest(api_key=value):
                headers = build_auth_headers(OPENAI, value, {})
                self.assertNotIn("Authorization", headers)
                self.assertEqual(headers["Accept"], "application/json")

    def test_unusable_key_is_ignored_rather_than_breaking_the_header(self) -> None:
        for value in ("sk-test\nInjected: x", "sk tést", "\u200b"):
            with self.subTest(api_key=value):
                headers = build_auth_headers(OPENAI, value, {})
                self.assertNotIn("Authorization", headers)

    def test_configured_headers_are_copied_and_never_mutated(self) -> None:
        configured = {"HTTP-Referer": "https://example.test", "X-Title": "dsh"}
        headers = build_auth_headers(OPENAI, "sk-test", configured)
        self.assertEqual(configured, {"HTTP-Referer": "https://example.test", "X-Title": "dsh"})
        self.assertEqual(headers["HTTP-Referer"], "https://example.test")
        self.assertEqual(headers["X-Title"], "dsh")
        self.assertEqual(headers["Authorization"], "Bearer sk-test")
        self.assertIsNot(headers, configured)

    def test_explicit_auth_header_is_not_overridden(self) -> None:
        cases = (
            (OPENAI, {"Authorization": "Bearer deployment-token"}),
            (OPENAI, {"authorization": "Bearer deployment-token"}),
            (ANTHROPIC, {"x-api-key": "deployment-key"}),
            (GEMINI, {"X-Goog-Api-Key": "deployment-key"}),
        )
        for api, configured in cases:
            with self.subTest(api=api, configured=configured):
                headers = build_auth_headers(api, "sk-ignored", dict(configured))
                merged = {key.lower(): value for key, value in headers.items()}
                name, value = next(iter(configured.items()))
                self.assertEqual(merged[name.lower()], value)
                self.assertNotIn("sk-ignored", headers.values())

    def test_configured_accept_header_is_kept(self) -> None:
        headers = build_auth_headers(OPENAI, None, {"accept": "application/vnd.test+json"})
        self.assertEqual(headers, {"accept": "application/vnd.test+json"})


# ---------------------------------------------------------------------------
# fetch_models
# ---------------------------------------------------------------------------
class FetchModelsTests(unittest.TestCase):
    def test_reads_the_data_array_with_capacities(self) -> None:
        opener = RecordingOpener(
            body=json_body(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "deepseek-v4-flash",
                            "name": "DeepSeek V4 Flash",
                            "context_length": 131072,
                            "max_output_tokens": 8192,
                        },
                        {"id": "glm-5.3"},
                    ],
                }
            )
        )
        result = fetch_models("https://h/v1/models", {"Authorization": "Bearer sk-x"}, 7.5, opener=opener)
        self.assertIsNone(result.error)
        self.assertTrue(result.ok)
        self.assertEqual(result.url, "https://h/v1/models")
        self.assertEqual(
            result.models,
            (
                RemoteModel(
                    id="deepseek-v4-flash",
                    name="DeepSeek V4 Flash",
                    contextWindow=131072,
                    maxTokens=8192,
                ),
                RemoteModel(id="glm-5.3"),
            ),
        )
        request, timeout = opener.calls[0]
        self.assertEqual(timeout, 7.5)
        self.assertEqual(request.method, "GET")
        self.assertEqual(header_value(request, "Authorization"), "Bearer sk-x")

    def test_reads_the_enriched_models_map(self) -> None:
        opener = RecordingOpener(
            body=json_body(
                {
                    "models": {
                        "vendor/model-a": {
                            "id": "canonical-a",
                            "display_name": "Model A",
                            "limit": {"context": 200000, "output": 64000},
                        },
                        "vendor/model-b": {"top_provider": {"max_completion_tokens": 4096}},
                        "ignored_primitive": 12,
                    }
                }
            )
        )
        result = fetch_models("https://h/v1/models", {}, 5, opener=opener)
        self.assertIsNone(result.error)
        self.assertEqual(
            result.models,
            (
                RemoteModel(id="vendor/model-a", name="Model A", contextWindow=200000, maxTokens=64000),
                RemoteModel(id="vendor/model-b", maxTokens=4096),
            ),
        )

    def test_reads_the_gemini_models_array(self) -> None:
        opener = RecordingOpener(
            body=json_body(
                {
                    "models": [
                        {
                            "name": "models/gemini-3-pro",
                            "displayName": "Gemini 3 Pro",
                            "inputTokenLimit": 1000000,
                            "outputTokenLimit": 65536,
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            "name": "models/text-embedding-004",
                            "supportedGenerationMethods": ["embedContent"],
                        },
                        {"name": "models/gemini-3-flash"},
                    ]
                }
            )
        )
        result = fetch_models("https://g/v1beta/models?pageSize=1000", {}, 5, opener=opener)
        self.assertIsNone(result.error)
        self.assertEqual(
            result.models,
            (
                RemoteModel(
                    id="gemini-3-pro",
                    name="Gemini 3 Pro",
                    contextWindow=1000000,
                    maxTokens=65536,
                ),
                RemoteModel(id="gemini-3-flash", name="gemini-3-flash"),
            ),
        )

    def test_capacity_field_variants(self) -> None:
        variants = (
            ({"context_window": 1000}, 1000),
            ({"max_input_tokens": 2000}, 2000),
            ({"contextWindow": 3000}, 3000),
            ({"inputTokenLimit": 4000}, 4000),
            ({"limit": {"context": 5000}}, 5000),
            ({"context_length": "6000"}, 6000),
        )
        for entry, expected in variants:
            with self.subTest(entry=entry):
                opener = RecordingOpener(body=json_body({"data": [dict(entry, id="m")]}))
                result = fetch_models("https://h/v1/models", {}, 5, opener=opener)
                self.assertEqual(result.models[0].contextWindow, expected)

    def test_zero_and_boolean_capacities_are_ignored(self) -> None:
        opener = RecordingOpener(
            body=json_body({"data": [{"id": "m", "contextWindow": 0, "maxTokens": True}]})
        )
        result = fetch_models("https://h/v1/models", {}, 5, opener=opener)
        self.assertEqual(result.models, (RemoteModel(id="m"),))

    def test_entries_without_an_id_are_skipped(self) -> None:
        opener = RecordingOpener(
            body=json_body({"data": [{"name": None}, {"id": "   "}, {"id": "ok"}, "bare-id", 7]})
        )
        result = fetch_models("https://h/v1/models", {}, 5, opener=opener)
        self.assertIsNone(result.error)
        self.assertEqual(
            result.models, (RemoteModel(id="ok"), RemoteModel(id="bare-id"))
        )

    def test_bare_list_payload_is_accepted(self) -> None:
        opener = RecordingOpener(body=json_body([{"id": "a"}, {"id": "b"}]))
        result = fetch_models("https://h/v1/models", {}, 5, opener=opener)
        self.assertEqual([model.id for model in result.models], ["a", "b"])

    def test_empty_listing_is_a_success_with_no_models(self) -> None:
        opener = RecordingOpener(body=json_body({"data": []}))
        result = fetch_models("https://h/v1/models", {}, 5, opener=opener)
        self.assertIsNone(result.error)
        self.assertEqual(result.models, ())

    def test_str_body_is_also_accepted(self) -> None:
        opener = RecordingOpener(body=json.dumps({"data": [{"id": "a"}]}))
        result = fetch_models("https://h/v1/models", {}, 5, opener=opener)
        self.assertEqual(result.models, (RemoteModel(id="a"),))

    def test_http_error_statuses_are_reported(self) -> None:
        cases = (
            (401, "check the API key"),
            (403, "check the API key"),
            (404, "check the base URL"),
            (429, "rate limited"),
            (503, "server error"),
            (418, "HTTP 418"),
        )
        for status, fragment in cases:
            with self.subTest(status=status):
                result = fetch_models("https://h/v1/models", {}, 5, opener=RecordingOpener(status=status))
                self.assertEqual(result.url, "https://h/v1/models")
                self.assertEqual(result.models, ())
                self.assertIsNotNone(result.error)
                self.assertIn(fragment, result.error)
                self.assertFalse(result.ok)
                self.assertEqual(result.meta["status"], status)

    def test_timeout_is_reported_without_raising(self) -> None:
        opener = RecordingOpener(error=client.TransportError("request timed out"))
        result = fetch_models("https://h/v1/models", {}, 5, opener=opener)
        self.assertEqual(result.error, "request timed out")

    def test_connection_error_is_reported_without_raising(self) -> None:
        opener = RecordingOpener(error=client.TransportError("could not reach the endpoint ([Errno 11001] getaddrinfo failed)"))
        result = fetch_models("https://h/v1/models", {}, 5, opener=opener)
        self.assertIn("could not reach the endpoint", result.error)

    def test_invalid_json_is_reported(self) -> None:
        opener = RecordingOpener(body=b"<html>gateway error</html>")
        result = fetch_models("https://h/v1/models", {}, 5, opener=opener)
        self.assertEqual(result.error, "the endpoint did not answer with JSON")

    def test_unrecognised_structure_is_reported(self) -> None:
        for payload in ({"foo": 1}, {"data": "nope"}, {"models": 3}, "a string", 42):
            with self.subTest(payload=payload):
                opener = RecordingOpener(body=json_body(payload))
                result = fetch_models("https://h/v1/models", {}, 5, opener=opener)
                self.assertIsNotNone(result.error)
                self.assertEqual(result.models, ())

    def test_empty_body_is_reported(self) -> None:
        for body in (b"", b"   \n", None):
            with self.subTest(body=body):
                opener = RecordingOpener(body=body)
                result = fetch_models("https://h/v1/models", {}, 5, opener=opener)
                self.assertEqual(result.error, "the endpoint returned an empty body")

    def test_data_array_wins_over_models_map(self) -> None:
        opener = RecordingOpener(
            body=json_body(
                {"data": [{"id": "from-data"}], "models": {"from-models": {"id": "x"}}}
            )
        )
        result = fetch_models("https://h/v1/models", {}, 5, opener=opener)
        self.assertEqual([model.id for model in result.models], ["from-data"])

    def test_entry_name_falls_back_to_the_display_variants(self) -> None:
        opener = RecordingOpener(
            body=json_body({"data": [{"id": "a", "display_name": "Thing A"}, {"id": "b"}]})
        )
        result = fetch_models("https://h/v1/models", {}, 5, opener=opener)
        self.assertEqual(
            result.models, (RemoteModel(id="a", name="Thing A"), RemoteModel(id="b"))
        )

    def test_blank_url_is_reported(self) -> None:
        opener = RecordingOpener()
        for url in ("", "   "):
            with self.subTest(url=url):
                result = fetch_models(url, {}, 5, opener=opener)
                self.assertEqual(result.error, "no endpoint url")
                self.assertEqual(result.url, "")
        self.assertEqual(opener.call_count, 0)

    def test_unexpected_exception_is_captured(self) -> None:
        opener = RecordingOpener(error=RuntimeError("socket exploded"))
        result = fetch_models("https://h/v1/models", {}, 5, opener=opener)
        self.assertIn("request failed", result.error)
        self.assertIn("socket exploded", result.error)

    def test_opener_contract_violation_is_reported(self) -> None:
        for reply in ("junk", (200,), None):
            with self.subTest(reply=reply):
                result = fetch_models("https://h/v1/models", {}, 5, opener=lambda request, timeout: reply)
                self.assertIsNotNone(result.error)
                self.assertIn("opener", result.error)

    def test_error_text_never_contains_the_key(self) -> None:
        secret = "sk-verysecretvalue123456"
        headers = {"Authorization": f"Bearer {secret}"}
        opener = RecordingOpener(error=RuntimeError(f"failed with {secret} in the header"))
        result = fetch_models("https://h/v1/models", headers, 5, opener=opener)
        self.assertIsNotNone(result.error)
        self.assertNotIn(secret, result.error)
        self.assertNotIn("Bearer", result.error)
        self.assertIn("<redacted>", result.error)
        self.assertNotIn("\n", result.error)

    def test_multiline_error_text_is_flattened(self) -> None:
        opener = RecordingOpener(error=RuntimeError("line one\nline two\nline three"))
        result = fetch_models("https://h/v1/models", {}, 5, opener=opener)
        self.assertEqual(result.error, "request failed: line one line two line three")


# ---------------------------------------------------------------------------
# default_opener contract
# ---------------------------------------------------------------------------
class DefaultOpenerTests(unittest.TestCase):
    def test_converts_a_response_into_status_and_bytes(self) -> None:
        response = FakeResponse(200, b'{"data": []}')
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            status, body = client.default_opener(urllib.request.Request("https://h/v1/models"), 3)
        self.assertEqual((status, body), (200, b'{"data": []}'))
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 3)

    def test_every_response_also_satisfies_fetch_models(self) -> None:
        response = FakeResponse(200, json_body({"data": [{"id": "m"}]}))
        with mock.patch("urllib.request.urlopen", return_value=response):
            result = fetch_models("https://h/v1/models", {}, 3)
        self.assertEqual(result.models, (RemoteModel(id="m"),))

    def test_http_error_status_is_returned_not_raised(self) -> None:
        error = urllib.error.HTTPError(
            "https://h/v1/models", 403, "Forbidden", {}, io.BytesIO(b'{"error": "nope"}')
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            status, body = client.default_opener(urllib.request.Request("https://h/v1/models"), 3)
        self.assertEqual(status, 403)
        self.assertEqual(body, b"")

    def test_timeout_is_raised_as_a_transport_error(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError(TimeoutError("timed out"))):
            with self.assertRaises(client.TransportError) as caught:
                client.default_opener(urllib.request.Request("https://h/v1/models"), 1)
        self.assertEqual(str(caught.exception), "request timed out")

    def test_connection_refusal_is_raised_as_a_transport_error(self) -> None:
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError(ConnectionRefusedError(111, "refused")),
        ):
            with self.assertRaises(client.TransportError) as caught:
                client.default_opener(urllib.request.Request("https://h/v1/models"), 1)
        self.assertIn("could not reach the endpoint", str(caught.exception))


# ---------------------------------------------------------------------------
# Balances
# ---------------------------------------------------------------------------
class CreditsTests(unittest.TestCase):
    def credits(self, base_url: str, payload: Any, status: int = 200) -> str | None:
        opener = RecordingOpener(status=status, body=json_body(payload))
        return fetch_credits(base_url, "sk-test", {}, 5, opener=opener)

    def test_known_hosts_match_the_documented_endpoints(self) -> None:
        cases = (
            ("https://api.deepseek.com/v1", "api.deepseek.com", "https://api.deepseek.com/user/balance", "CNY"),
            ("https://openrouter.ai/api/v1", "openrouter.ai", "https://openrouter.ai/api/v1/credits", "USD"),
            ("https://api.moonshot.cn/v1", "api.moonshot.cn", "https://api.moonshot.cn/v1/users/me/balance", "CNY"),
            ("https://api.moonshot.ai/v1", "api.moonshot.ai", "https://api.moonshot.ai/v1/users/me/balance", "USD"),
            ("https://api.stepfun.com/v1", "api.stepfun.com", "https://api.stepfun.com/v1/accounts", "CNY"),
            ("https://api.novita.ai/v3", "api.novita.ai", "https://api.novita.ai/v3/user/balance", "USD"),
        )
        for base_url, host, url, currency in cases:
            with self.subTest(base_url=base_url):
                self.assertEqual(known_credit_host(base_url), host)
                self.assertEqual(credits_endpoint(base_url), (url, currency))

    def test_unknown_hosts_are_not_queryable(self) -> None:
        for base_url in ("https://api.example.com/v1", "", "   ", "https://unknown.test"):
            with self.subTest(base_url=base_url):
                self.assertIsNone(known_credit_host(base_url))
                self.assertIsNone(credits_endpoint(base_url))

    def test_host_matching_ignores_port_case_and_a_bare_host(self) -> None:
        for base_url in (
            "https://api.deepseek.com:443/v1",
            "HTTPS://API.DeepSeek.com/v1",
            "api.deepseek.com/v1",
        ):
            with self.subTest(base_url=base_url):
                self.assertEqual(known_credit_host(base_url), "api.deepseek.com")

    def test_unknown_host_makes_no_request(self) -> None:
        opener = RecordingOpener(body=json_body({"balance": 1}))
        self.assertIsNone(fetch_credits("https://api.example.com/v1", "sk", {}, 5, opener=opener))
        self.assertEqual(opener.call_count, 0)

    def test_deepseek_balance_uses_the_reported_currency(self) -> None:
        payload = {"balance_infos": [{"currency": "CNY", "total_balance": "12.34"}]}
        self.assertEqual(self.credits("https://api.deepseek.com/v1", payload), "CNY 12.34")

    def test_deepseek_balance_falls_back_to_cny(self) -> None:
        payload = {"balance_infos": [{"total_balance": 12.3}]}
        self.assertEqual(self.credits("https://api.deepseek.com/v1", payload), "CNY 12.30")

    def test_openrouter_reports_remaining_credits(self) -> None:
        payload = {"data": {"total_credits": 10, "total_usage": 6.5}}
        self.assertEqual(self.credits("https://openrouter.ai/api/v1", payload), "USD 3.50")

    def test_moonshot_balances_use_their_own_currency(self) -> None:
        payload = {"data": {"available_balance": 3.5}}
        self.assertEqual(self.credits("https://api.moonshot.cn/v1", payload), "CNY 3.50")
        self.assertEqual(self.credits("https://api.moonshot.ai/v1", payload), "USD 3.50")

    def test_stepfun_and_novita_scaling(self) -> None:
        self.assertEqual(self.credits("https://api.stepfun.com/v1", {"balance": 7}), "CNY 7.00")
        self.assertEqual(
            self.credits("https://api.novita.ai/v3", {"availableBalance": 350000}), "USD 35.00"
        )

    def test_balance_request_carries_the_bearer_header(self) -> None:
        opener = RecordingOpener(status=200, body=json_body({"balance": 1}))
        fetch_credits("https://api.stepfun.com/v1", "sk-test", {"X-Title": "dsh"}, 5, opener=opener)
        request, _ = opener.calls[0]
        self.assertEqual(request.full_url, "https://api.stepfun.com/v1/accounts")
        self.assertEqual(header_value(request, "Authorization"), "Bearer sk-test")
        self.assertEqual(header_value(request, "X-Title"), "dsh")

    def test_failures_and_unreadable_bodies_return_none(self) -> None:
        payloads = (
            (500, {"balance_infos": [{"total_balance": 1}]}),
            (401, {"data": {"available_balance": 1}}),
        )
        for status, payload in payloads:
            with self.subTest(status=status):
                self.assertIsNone(self.credits("https://api.deepseek.com/v1", payload, status=status))
        self.assertIsNone(self.credits("https://api.deepseek.com/v1", {"unexpected": True}))
        self.assertIsNone(self.credits("https://api.deepseek.com/v1", {"balance_infos": []}))

    def test_transport_failure_returns_none(self) -> None:
        opener = RecordingOpener(error=client.TransportError("request timed out"))
        self.assertIsNone(fetch_credits("https://api.deepseek.com/v1", "sk", {}, 5, opener=opener))

    def test_non_json_body_returns_none(self) -> None:
        opener = RecordingOpener(body=b"<html>nope</html>")
        self.assertIsNone(fetch_credits("https://api.deepseek.com/v1", "sk", {}, 5, opener=opener))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
