"""Tests for :mod:`dsh_byok_sync.credentials`.

Every test builds its own temporary ``.credentials.yaml``; the real credential
file is never opened. Secrets below are obvious fakes.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

try:  # ``python -m unittest discover -s tests`` puts tests/ on sys.path
    import _helpers
except ModuleNotFoundError:  # ``python -m unittest tests.test_credentials`` does not
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _helpers

from dsh_byok_sync import credentials as credentials_module
from dsh_byok_sync.credentials import Credentials, default_path

DOCUMENT = """\
version: 1
records:
  client-connection/browser-session:
    kind: secret
    payload:
      version: 1
      secret: session-secret-value
  llm-pi-ai/iris:
    kind: api-key
    payload:
      apiKey: record-iris-key
  llm-pi-ai/nvidia:
    kind: api-key
    payload:
      key: record-nvidia-key
  llm-pi-ai/moonshot:
    kind: api-key
    payload:
      token: record-moonshot-key
  llm-pi-ai/stepfun:
    kind: api-key
    payload:
      secret: record-stepfun-key
refs:
  IRIS_API_KEY: ref-iris-key
  NVIDIA_API_KEY: ref-nvidia-key
  EMPTY_REF: ""
"""

#: Values that must never appear in ``repr``/``str`` output.
SECRETS = (
    "ref-iris-key",
    "ref-nvidia-key",
    "record-iris-key",
    "record-nvidia-key",
    "record-moonshot-key",
    "record-stepfun-key",
    "session-secret-value",
    "env-iris-key",
)


class CredentialsTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _helpers.make_tmpdir()
        self.addCleanup(_helpers.cleanup, self.tmp)

    def write(self, text: str, name: str = ".credentials.yaml", newline: str = "\n") -> Path:
        return _helpers.write_file(self.tmp / name, text, newline=newline)

    def load(self, text: str = DOCUMENT, environ: object = None, newline: str = "\n") -> Credentials:
        path = self.write(text, newline=newline)
        return Credentials.load(path=path, environ={} if environ is None else environ)


class ResolutionOrderTests(CredentialsTestBase):
    def test_environment_wins_over_refs(self) -> None:
        store = self.load(environ={"IRIS_API_KEY": "env-iris-key"})
        self.assertEqual(store.resolve("IRIS_API_KEY", "iris"), "env-iris-key")

    def test_refs_are_used_when_the_environment_has_nothing(self) -> None:
        store = self.load()
        self.assertEqual(store.resolve("IRIS_API_KEY", "iris"), "ref-iris-key")
        self.assertEqual(store.resolve("NVIDIA_API_KEY", "nvidia"), "ref-nvidia-key")

    def test_blank_environment_value_falls_through_to_refs(self) -> None:
        store = self.load(environ={"IRIS_API_KEY": "   "})
        self.assertEqual(store.resolve("IRIS_API_KEY", "iris"), "ref-iris-key")

    def test_records_are_the_last_resort_by_route(self) -> None:
        store = self.load()
        self.assertEqual(store.resolve("IRIS_UNSET_KEY", "iris"), "record-iris-key")

    def test_every_payload_key_name_is_read(self) -> None:
        store = self.load()
        cases = {
            "iris": "record-iris-key",  # apiKey
            "nvidia": "record-nvidia-key",  # key
            "moonshot": "record-moonshot-key",  # token
            "stepfun": "record-stepfun-key",  # secret
        }
        for route, expected in cases.items():
            with self.subTest(route=route):
                self.assertEqual(store.resolve("SOME_UNSET_KEY", route), expected)

    def test_payload_keys_are_matched_case_insensitively(self) -> None:
        text = (
            "records:\n"
            "  llm-pi-ai/lower:\n"
            "    payload:\n"
            "      apikey: lower-key\n"
            "refs: {}\n"
        )
        store = self.load(text)
        self.assertEqual(store.resolve("UNSET", "lower"), "lower-key")

    def test_no_api_key_env_means_no_key(self) -> None:
        store = self.load(environ={"IRIS_API_KEY": "env-iris-key"})
        for value in (None, "", "   "):
            with self.subTest(api_key_env=value):
                self.assertIsNone(store.resolve(value, "iris"))

    def test_unknown_route_and_unknown_env_resolve_to_none(self) -> None:
        store = self.load()
        self.assertIsNone(store.resolve("TOTALLY_UNSET_KEY", "no-such-route"))
        self.assertIsNone(store.resolve("TOTALLY_UNSET_KEY", ""))

    def test_environment_is_snapshotted_at_load(self) -> None:
        environ = {"IRIS_API_KEY": "env-iris-key"}
        store = self.load(environ=environ)
        environ["IRIS_API_KEY"] = "later-value"
        self.assertEqual(store.resolve("IRIS_API_KEY", "iris"), "env-iris-key")

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        store = self.load(environ={"IRIS_API_KEY": "  spaced-key  "})
        self.assertEqual(store.resolve("IRIS_API_KEY", "iris"), "spaced-key")

    def test_a_route_without_a_record_falls_through_to_none(self) -> None:
        store = self.load()
        self.assertIsNone(store.resolve("UNSET_KEY", "openrouter"))


class ToleranceTests(CredentialsTestBase):
    def test_missing_file_is_tolerated(self) -> None:
        store = Credentials.load(path=self.tmp / "does-not-exist.yaml", environ={})
        self.assertFalse(store.loaded)
        self.assertIn("unavailable", store.error)
        self.assertIsNone(store.resolve("IRIS_API_KEY", "iris"))
        self.assertEqual(store.ref_names, ())

    def test_a_directory_path_is_tolerated(self) -> None:
        store = Credentials.load(path=self.tmp, environ={})
        self.assertFalse(store.loaded)
        self.assertIsNotNone(store.error)

    def test_empty_file_is_a_valid_empty_store(self) -> None:
        store = self.load("")
        self.assertTrue(store.loaded)
        self.assertIsNone(store.error)
        self.assertIsNone(store.resolve("IRIS_API_KEY", "iris"))

    def test_comment_only_file_is_a_valid_empty_store(self) -> None:
        store = self.load("# nothing here\n\n  # still nothing\n")
        self.assertTrue(store.loaded)
        self.assertEqual(store.ref_names, ())

    def test_tab_indentation_is_refused_not_misread(self) -> None:
        store = self.load("refs:\n\tIRIS_API_KEY: tabbed\n")
        self.assertFalse(store.loaded)
        self.assertIn("tab", store.error)
        self.assertIsNone(store.resolve("IRIS_API_KEY", "iris"))

    def test_non_utf8_bytes_are_tolerated(self) -> None:
        path = self.tmp / "binary.yaml"
        path.write_bytes(b"\xff\xfe\x00\x01not utf-8")
        store = Credentials.load(path=path, environ={})
        self.assertFalse(store.loaded)
        self.assertIn("UTF-8", store.error)

    def test_non_mapping_document_is_tolerated(self) -> None:
        store = self.load("- just\n- a\n- list\n")
        self.assertTrue(store.loaded)
        self.assertEqual(store.ref_names, ())
        self.assertIsNone(store.resolve("IRIS_API_KEY", "iris"))

    def test_garbage_content_does_not_raise(self) -> None:
        store = self.load("refs\nIRIS_API_KEY: nope\n: weird\n  - : :\n")
        self.assertIsNone(store.resolve("IRIS_API_KEY", "iris"))

    def test_records_payload_must_be_a_mapping(self) -> None:
        text = "records:\n  llm-pi-ai/iris:\n    payload: not-a-mapping\nrefs: {}\n"
        store = self.load(text)
        self.assertIsNone(store.resolve("UNSET_KEY", "iris"))

    def test_non_scalar_refs_are_skipped(self) -> None:
        text = "refs:\n  NESTED:\n    inner: value\n  PLAIN: plain-value\n"
        store = self.load(text)
        self.assertEqual(store.ref_names, ("PLAIN",))
        self.assertIsNone(store.resolve("NESTED", "iris"))
        self.assertEqual(store.resolve("PLAIN", "iris"), "plain-value")

    def test_empty_ref_value_does_not_shadow_a_record(self) -> None:
        store = self.load()
        self.assertEqual(store.resolve("EMPTY_REF", "iris"), "record-iris-key")


class ParsingTests(CredentialsTestBase):
    def test_quoted_keys_and_values_are_unquoted(self) -> None:
        text = (
            "refs:\n"
            '  "QUOTED_API_KEY": "quoted-key"\n'
            "  'SINGLE_API_KEY': 'single-key'\n"
        )
        store = self.load(text)
        self.assertEqual(store.resolve("QUOTED_API_KEY", "x"), "quoted-key")
        self.assertEqual(store.resolve("SINGLE_API_KEY", "x"), "single-key")

    def test_quoted_record_ids_are_unquoted(self) -> None:
        text = (
            "records:\n"
            '  "llm-pi-ai/quoted-route":\n'
            "    payload:\n"
            '      apiKey: "quoted-record-key"\n'
            "refs: {}\n"
        )
        store = self.load(text)
        self.assertIn("llm-pi-ai/quoted-route", store.record_ids)
        self.assertEqual(store.resolve("UNSET", "quoted-route"), "quoted-record-key")

    def test_single_quote_escape_is_handled(self) -> None:
        store = self.load("refs:\n  QUOTED: 'it''s-a-key'\n")
        self.assertEqual(store.resolve("QUOTED", "x"), "it's-a-key")

    def test_inline_comments_are_stripped(self) -> None:
        store = self.load("refs:\n  COMMENTED: key-with-comment  # a note\n")
        self.assertEqual(store.resolve("COMMENTED", "x"), "key-with-comment")

    def test_crlf_files_parse(self) -> None:
        store = self.load(DOCUMENT, newline="\r\n")
        self.assertEqual(store.resolve("IRIS_API_KEY", "iris"), "ref-iris-key")

    def test_utf8_bom_is_ignored(self) -> None:
        path = self.tmp / "bom.yaml"
        path.write_bytes(b"\xef\xbb\xbf" + DOCUMENT.encode("utf-8"))
        store = Credentials.load(path=path, environ={})
        self.assertTrue(store.loaded)
        self.assertEqual(store.resolve("IRIS_API_KEY", "iris"), "ref-iris-key")

    def test_trailing_colons_in_values_do_not_split_the_entry(self) -> None:
        store = self.load("refs:\n  URLISH: https://host:8443/key\n")
        self.assertEqual(store.resolve("URLISH", "x"), "https://host:8443/key")

    def test_document_markers_are_skipped(self) -> None:
        store = self.load("---\nrefs:\n  MARKED: marked-key\n...\n")
        self.assertEqual(store.resolve("MARKED", "x"), "marked-key")


class InspectionTests(CredentialsTestBase):
    def test_default_path_matches_dsh(self) -> None:
        self.assertEqual(default_path(), Path.home() / ".dsh" / ".credentials.yaml")

    def test_load_uses_the_default_path_when_none_is_given(self) -> None:
        path = self.write(DOCUMENT, name="fake-home-credentials.yaml")
        with mock.patch.object(credentials_module, "default_path", return_value=path) as patched:
            store = credentials_module.Credentials.load(environ={})
        patched.assert_called_once_with()
        self.assertEqual(store.path, path)
        self.assertEqual(store.resolve("IRIS_API_KEY", "iris"), "ref-iris-key")

    def test_explicit_path_is_recorded(self) -> None:
        path = self.write(DOCUMENT, name="explicit.yaml")
        store = Credentials.load(path=str(path), environ={})
        self.assertEqual(store.path, path)

    def test_names_and_ids_are_exposed_without_values(self) -> None:
        store = self.load()
        # ``EMPTY_REF`` is blank in the document, so it is not a usable reference.
        self.assertEqual(store.ref_names, ("IRIS_API_KEY", "NVIDIA_API_KEY"))
        self.assertIn("llm-pi-ai/iris", store.record_ids)
        self.assertIn("client-connection/browser-session", store.record_ids)

    def test_repr_and_str_never_leak_a_secret(self) -> None:
        store = self.load(environ={"IRIS_API_KEY": "env-iris-key"})
        rendered = (repr(store), str(store), repr(store.__class__))
        for secret in SECRETS:
            for text in rendered:
                with self.subTest(secret=secret):
                    self.assertNotIn(secret, text)
        self.assertIn("Credentials(", rendered[0])
        self.assertIn("refs=2", rendered[0])
        self.assertIn("records=5", rendered[0])

    def test_error_message_never_leaks_a_secret(self) -> None:
        store = self.load("refs:\n\tIRIS_API_KEY: ref-iris-key\n")
        self.assertNotIn("ref-iris-key", str(store.error))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
