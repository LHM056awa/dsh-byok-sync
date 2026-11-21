"""Tests for :mod:`dsh_byok_sync.configfile`.

Covers the contract in SPEC.md §3.3: the ``resolve_config`` probe order, loading
both DSH file shapes (``cordis.patch.yml`` list root and ``settings.yaml``
mapping root), CRLF / missing-final-newline handling, undetectable shapes, and
the atomic writer.

Everything runs against workspace-local temporary directories
(:func:`tests._helpers.make_tmpdir`) and a synthetic environment; no real DSH
profile, settings file, or credential file is ever read.
"""

from __future__ import annotations

import contextlib
import os
import unittest
from pathlib import Path
from unittest.mock import patch

try:  # `python -m unittest discover -s tests`
    import _helpers
except ImportError:  # `python -m unittest tests.test_configfile`
    from tests import _helpers

from dsh_byok_sync.configfile import (
    load_document,
    resolve_config,
    write_text_atomic,
)
from dsh_byok_sync.models import ModelSyncError

#: Environment variables that influence config discovery. Every test starts
#: from a synthetic environment where none of them is set.
_ENV_KEYS = (
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "DSH_HOME",
    "DSH_PROFILE_HOME",
    "DSH_PROFILE",
    "DSH_PROFILE_DIR",
)


@contextlib.contextmanager
def clean_dsh_env(**values):
    """Run a block with only the DSH variables in *values* set."""
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    try:
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        for key, value in values.items():
            os.environ[key] = str(value)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextlib.contextmanager
def working_directory(path: Path):
    """Temporarily chdir so relative-path handling can be exercised."""
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


#: ``cordis.patch.yml``: list root, one plugin row per entry.
PATCH_TEXT = """\
# DSH profile patch (test fixture)
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      iris:
        apiKeyEnv: IRIS_API_KEY
        api: openai-completions
        baseURL: https://gateway.example.com/v1
        models:
          - id: deepseek-v4-flash
            name: deepseek-v4-flash
            contextWindow: 1000000
      nvidia:
        api: openai-responses
        baseURL: https://integrate.api.nvidia.com/v1
        models: []
"""


#: ``settings.yaml``: mapping root, ``llm-pi-ai`` at the top level.
SETTINGS_TEXT = """\
llm-pi-ai:
  providers:
    iris:
      apiKeyEnv: IRIS_API_KEY
      api: openai-completions
      baseURL: https://gateway.example.com/v1
      models:
        - id: deepseek-v4-flash
          name: deepseek-v4-flash
"""


#: A patch file with extra route fields and entries that are not usable models.
MESSY_TEXT = """\
- id: llm-pi-ai
  config:
    providers:
      iris:
        apiKeyEnv: IRIS_API_KEY
        api: openai-completions
        baseURL: https://gateway.example.com/v1
        headers:
          X-Title: dsh-byok-sync
        models:
          - id: keep-me
            name: Keep Me
          - name: broken-entry
          - just-a-string
"""


#: A patch file whose plugin row has a different id (``--entry`` target).
CUSTOM_ENTRY_TEXT = """\
- id: other-plugin
  config:
    providers:
      routed:
        baseURL: https://example.test/v1
        models:
          - id: only-one
"""


#: A route without a ``models`` key at all.
NO_MODELS_TEXT = """\
- id: llm-pi-ai
  config:
    providers:
      bare:
        api: openai-completions
        baseURL: https://bare.test/v1
"""


def patch_with_models(block: str) -> str:
    """A patch file whose ``iris`` route ends with the given ``models`` snippet."""
    return (
        "- id: llm-pi-ai\n"
        "  config:\n"
        "    providers:\n"
        "      iris:\n"
        "        api: openai-completions\n"
        "        baseURL: https://x.test/v1\n" + block
    )


class _TmpdirCase(unittest.TestCase):
    """Shared workspace-local temp directory plumbing."""

    def setUp(self):
        self.dir = _helpers.make_tmpdir()
        self.addCleanup(lambda: _helpers.cleanup(self.dir))
        self.home = self.dir / "home"
        self.home.mkdir()
        self.profile_dir = self.dir / "profile"
        self.profile_dir.mkdir()

    def write(self, path: Path, text: str = PATCH_TEXT, newline: str = "\n") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        return _helpers.write_file(path, text, newline=newline)

    def load(self, text: str, name: str = "cordis.patch.yml", newline: str = "\n", **kwargs):
        path = self.write(self.dir / name, text, newline=newline)
        return load_document(path, **kwargs), path


class ResolveConfigTest(_TmpdirCase):
    """SPEC §3.3 probe order: explicit, $DSH_PROFILE_DIR, profile dir, settings."""

    def test_explicit_path_wins_over_every_probe(self):
        explicit = self.write(self.dir / "explicit.yml")
        self.write(self.profile_dir / "cordis.patch.yml")
        self.write(self.home / "settings.yaml", SETTINGS_TEXT)
        with clean_dsh_env(DSH_PROFILE_DIR=self.profile_dir, DSH_HOME=self.home):
            resolved = resolve_config(str(explicit), None)
        self.assertEqual(explicit.resolve(), Path(resolved).resolve())

    def test_explicit_relative_path_resolves_to_same_file(self):
        explicit = self.write(self.dir / "explicit.yml")
        with working_directory(self.dir), clean_dsh_env(DSH_HOME=self.home):
            resolved = resolve_config("explicit.yml", None)
            # Resolve while the relative path still refers to the chdir target.
            resolved_absolute = Path(resolved).resolve()
            self.assertTrue(Path(resolved).is_file())
        self.assertEqual(explicit.resolve(), resolved_absolute)

    def test_explicit_missing_file_raises(self):
        missing = self.dir / "nope.yml"
        with clean_dsh_env(DSH_HOME=self.home):
            with self.assertRaises(ModelSyncError):
                resolve_config(str(missing), None)

    def test_profile_dir_env_is_used(self):
        target = self.write(self.profile_dir / "cordis.patch.yml")
        with clean_dsh_env(DSH_PROFILE_DIR=self.profile_dir, DSH_HOME=self.home):
            resolved = resolve_config(None, None)
        self.assertEqual(target.resolve(), Path(resolved).resolve())

    def test_profile_dir_env_accepts_relative_path(self):
        target = self.write(self.dir / "relprof" / "cordis.patch.yml")
        with working_directory(self.dir), clean_dsh_env(
            DSH_PROFILE_DIR="relprof", DSH_HOME=self.home
        ):
            resolved = Path(resolve_config(None, None)).resolve()
        self.assertEqual(target.resolve(), resolved)

    def test_profile_argument_is_used(self):
        target = self.write(self.home / "profiles" / "alpha" / "cordis.patch.yml")
        with clean_dsh_env(DSH_HOME=self.home):
            resolved = resolve_config(None, "alpha")
        self.assertEqual(target.resolve(), Path(resolved).resolve())

    def test_profile_argument_beats_dsh_profile_env(self):
        alpha = self.write(self.home / "profiles" / "alpha" / "cordis.patch.yml")
        self.write(self.home / "profiles" / "beta" / "cordis.patch.yml")
        with clean_dsh_env(DSH_HOME=self.home, DSH_PROFILE="beta"):
            resolved = resolve_config(None, "alpha")
        self.assertEqual(alpha.resolve(), Path(resolved).resolve())

    def test_dsh_profile_env_is_used_without_argument(self):
        beta = self.write(self.home / "profiles" / "beta" / "cordis.patch.yml")
        self.write(self.home / "profiles" / "desktop" / "cordis.patch.yml")
        with clean_dsh_env(DSH_HOME=self.home, DSH_PROFILE="beta"):
            resolved = resolve_config(None, None)
        self.assertEqual(beta.resolve(), Path(resolved).resolve())

    def test_default_profile_is_desktop(self):
        desktop = self.write(self.home / "profiles" / "desktop" / "cordis.patch.yml")
        with clean_dsh_env(DSH_HOME=self.home):
            resolved = resolve_config(None, None)
        self.assertEqual(desktop.resolve(), Path(resolved).resolve())

    def test_profile_dir_env_beats_profile_argument(self):
        env_file = self.write(self.profile_dir / "cordis.patch.yml")
        self.write(self.home / "profiles" / "alpha" / "cordis.patch.yml")
        with clean_dsh_env(DSH_PROFILE_DIR=self.profile_dir, DSH_HOME=self.home):
            resolved = resolve_config(None, "alpha")
        self.assertEqual(env_file.resolve(), Path(resolved).resolve())

    def test_settings_yaml_used_when_profile_dir_is_absent(self):
        """SPEC §3.3 case 4: no profile dir at all still falls back to settings."""
        settings = self.write(self.home / "settings.yaml", SETTINGS_TEXT)
        with clean_dsh_env(DSH_HOME=self.home):
            resolved = resolve_config(None, "ghost-profile")
        self.assertEqual(settings.resolve(), Path(resolved).resolve())

    def test_settings_yaml_used_when_profile_has_no_patch_file(self):
        (self.home / "profiles" / "desktop").mkdir(parents=True)
        settings = self.write(self.home / "settings.yaml", SETTINGS_TEXT)
        with clean_dsh_env(DSH_HOME=self.home):
            resolved = resolve_config(None, None)
        self.assertEqual(settings.resolve(), Path(resolved).resolve())

    def test_nothing_found_raises(self):
        # The profile dir exists but holds no patch file, and there is no
        # settings.yaml anywhere: the tool must refuse to guess.
        with clean_dsh_env(DSH_PROFILE_DIR=self.profile_dir, DSH_HOME=self.home):
            with self.assertRaises(ModelSyncError):
                resolve_config(None, None)

    def test_returns_a_path_instance(self):
        target = self.write(self.profile_dir / "cordis.patch.yml")
        with clean_dsh_env(DSH_PROFILE_DIR=self.profile_dir, DSH_HOME=self.home):
            resolved = resolve_config(None, None)
        self.assertIsInstance(resolved, Path)
        self.assertEqual(target.resolve(), resolved.resolve())


class LoadDocumentTest(_TmpdirCase):
    """SPEC §3.3: both file shapes parse into one ``Document``."""

    def test_patch_file_shape(self):
        doc, _path = self.load(PATCH_TEXT)
        self.assertEqual("patch", doc.kind)
        self.assertEqual("llm-pi-ai", doc.entry_id)

    def test_patch_file_route_fields(self):
        doc, _path = self.load(PATCH_TEXT)
        self.assertEqual(["iris", "nvidia"], list(doc.routes))
        iris = doc.routes["iris"]
        self.assertEqual("iris", iris.route)
        self.assertEqual("openai-completions", iris.api)
        self.assertEqual("https://gateway.example.com/v1", iris.base_url)
        self.assertEqual("IRIS_API_KEY", iris.api_key_env)

    def test_patch_file_models_are_raw_mappings(self):
        doc, _path = self.load(PATCH_TEXT)
        models = doc.routes["iris"].models
        self.assertEqual(1, len(models))
        entry = models[0]
        self.assertEqual("deepseek-v4-flash", entry["id"])
        self.assertEqual("deepseek-v4-flash", entry["name"])
        self.assertEqual(1000000, entry["contextWindow"])

    def test_flow_style_empty_models_list(self):
        doc, _path = self.load(PATCH_TEXT)
        self.assertEqual(0, len(doc.routes["nvidia"].models))
        self.assertEqual("openai-responses", doc.routes["nvidia"].api)

    def test_settings_file_shape(self):
        doc, _path = self.load(SETTINGS_TEXT, name="settings.yaml")
        self.assertEqual("settings", doc.kind)
        self.assertEqual(["iris"], list(doc.routes))
        models = doc.routes["iris"].models
        self.assertEqual("deepseek-v4-flash", models[0]["id"])

    def test_settings_file_parses_after_crlf_roundtrip(self):
        doc, path = self.load(SETTINGS_TEXT, name="settings.yaml", newline="\r\n")
        self.assertEqual("settings", doc.kind)
        self.assertEqual("\r\n", doc.newline)
        self.assertIn("\r\n", path.read_bytes().decode("utf-8"))

    def test_custom_entry_id_is_selectable(self):
        doc, _path = self.load(CUSTOM_ENTRY_TEXT, entry="other-plugin")
        self.assertEqual("other-plugin", doc.entry_id)
        self.assertEqual(["routed"], list(doc.routes))
        self.assertEqual("only-one", doc.routes["routed"].models[0]["id"])

    def test_missing_entry_raises(self):
        # The file parses, but the default entry has no providers block.
        with self.assertRaises(ModelSyncError):
            self.load(CUSTOM_ENTRY_TEXT)

    def test_lf_newline_detected(self):
        doc, _path = self.load(PATCH_TEXT)
        self.assertEqual("\n", doc.newline)

    def test_crlf_newline_detected(self):
        doc, _path = self.load(PATCH_TEXT, newline="\r\n")
        self.assertEqual("\r\n", doc.newline)
        self.assertIn("\r\n", doc.text)

    def test_text_is_kept_verbatim(self):
        doc, _path = self.load(PATCH_TEXT, newline="\r\n")
        self.assertEqual(PATCH_TEXT.replace("\n", "\r\n"), doc.text)

    def test_file_without_final_newline_parses(self):
        doc, _path = self.load(PATCH_TEXT.rstrip("\n"))
        self.assertFalse(doc.text.endswith("\n"))
        self.assertEqual(["iris", "nvidia"], list(doc.routes))

    def test_headers_mapping_is_preserved(self):
        doc, _path = self.load(MESSY_TEXT)
        self.assertEqual({"X-Title": "dsh-byok-sync"}, dict(doc.routes["iris"].headers))

    def test_invalid_model_entries_are_kept_for_the_sync_layer(self):
        doc, _path = self.load(MESSY_TEXT)
        models = doc.routes["iris"].models
        self.assertEqual(3, len(models))
        self.assertEqual("keep-me", models[0]["id"])
        self.assertNotIn("id", models[1])
        self.assertFalse(isinstance(models[2], dict))

    def test_route_without_models_key_has_empty_models(self):
        doc, _path = self.load(NO_MODELS_TEXT)
        self.assertEqual(0, len(doc.routes["bare"].models))
        self.assertEqual("https://bare.test/v1", doc.routes["bare"].base_url)

    def test_route_config_reports_presence(self):
        """SPEC §3.3 lists ``present: bool`` on ``RouteConfig``."""
        doc, _path = self.load(PATCH_TEXT)
        self.assertTrue(doc.routes["iris"].present)

    # --- SPEC §10: shapes that cannot be rewritten safely ------------------

    def test_non_empty_flow_models_is_reported_as_unsafe(self):
        doc, _path = self.load(patch_with_models("        models: [alpha, beta]\n"))
        cfg = doc.routes["iris"]
        self.assertTrue(cfg.models_error)
        self.assertIn("流风格", cfg.models_error)

    def test_non_empty_flow_models_are_not_exposed_as_usable_entries(self):
        # The flow-parsed items must not leak into the sync layer as local
        # models: this route is refused wholesale.
        doc, _path = self.load(patch_with_models("        models: [alpha, beta]\n"))
        self.assertEqual((), doc.routes["iris"].models)

    def test_empty_flow_models_is_allowed(self):
        # ``models: []`` holds nothing to preserve, so it may be rebuilt as a
        # block sequence later on.
        doc, _path = self.load(patch_with_models("        models: []\n"))
        cfg = doc.routes["iris"]
        self.assertIsNone(cfg.models_error)
        self.assertEqual(0, len(cfg.models))

    def test_null_models_value_is_allowed(self):
        # A bare ``models:`` (null) is the same as "nothing configured yet".
        doc, _path = self.load(patch_with_models("        models:\n"))
        cfg = doc.routes["iris"]
        self.assertIsNone(cfg.models_error)
        self.assertEqual(0, len(cfg.models))

    def test_models_mapping_value_sets_models_error(self):
        doc, _path = self.load(
            patch_with_models("        models:\n          alpha:\n            name: Alpha\n")
        )
        cfg = doc.routes["iris"]
        self.assertTrue(cfg.models_error)
        self.assertEqual((), cfg.models)

    def test_models_scalar_value_sets_models_error(self):
        doc, _path = self.load(patch_with_models("        models: not-a-list\n"))
        self.assertTrue(doc.routes["iris"].models_error)

    def test_models_block_scalar_value_sets_models_error(self):
        doc, _path = self.load(patch_with_models("        models: |\n          - id: alpha\n"))
        self.assertTrue(
            doc.routes["iris"].models_error,
            "块标量必须被拒绝，不能当成没有 models 键（否则同步会写坏字面块）",
        )

    def test_missing_file_raises_model_sync_error(self):
        with self.assertRaises(ModelSyncError):
            load_document(self.dir / "does-not-exist.yml")

    def test_undetectable_scalar_shape_raises(self):
        with self.assertRaises(ModelSyncError):
            self.load("just-a-string\n", name="weird.yml")

    def test_empty_file_raises(self):
        with self.assertRaises(ModelSyncError):
            self.load("", name="empty.yml")

    def test_document_keeps_its_path(self):
        doc, path = self.load(PATCH_TEXT)
        self.assertEqual(path.resolve(), Path(doc.path).resolve())


class WriteTextAtomicTest(_TmpdirCase):
    """SPEC §3.3: same-directory temp file + ``os.replace``."""

    def test_lf_content_is_written_byte_for_byte(self):
        path = self.write(self.dir / "cordis.patch.yml", "a: 1\nb: 2\n")
        write_text_atomic(path, "a: 1\nb: 3\n")
        self.assertEqual(b"a: 1\nb: 3\n", path.read_bytes())

    def test_crlf_style_is_preserved(self):
        path = self.write(self.dir / "cordis.patch.yml", "a: 1\r\nb: 2\r\n")
        write_text_atomic(path, "a: 1\r\nb: 3\r\n")
        self.assertEqual(b"a: 1\r\nb: 3\r\n", path.read_bytes())
        self.assertNotIn(b"\n", path.read_bytes().replace(b"\r\n", b""))

    def test_missing_final_newline_is_preserved(self):
        path = self.write(self.dir / "cordis.patch.yml", "a: 1\nb: 2")
        write_text_atomic(path, "a: 1\nb: 3")
        self.assertEqual(b"a: 1\nb: 3", path.read_bytes())

    def test_no_temporary_file_is_left_behind(self):
        target_dir = self.dir / "io"
        target_dir.mkdir()
        path = self.write(target_dir / "cordis.patch.yml", "a: 1\n")
        write_text_atomic(path, "a: 2\n")
        leftovers = [item.name for item in target_dir.iterdir() if item.name != path.name]
        self.assertEqual([], leftovers)

    def test_failed_replace_leaves_the_original_intact(self):
        path = self.write(self.dir / "cordis.patch.yml", "a: 1\nb: 2\n")
        original = path.read_bytes()
        with patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises((ModelSyncError, OSError)):
                write_text_atomic(path, "a: 9\n")
        self.assertEqual(original, path.read_bytes())

    def test_creates_a_file_that_does_not_exist_yet(self):
        target = self.dir / "fresh.yml"
        write_text_atomic(target, "a: 1\n")
        self.assertEqual(b"a: 1\n", target.read_bytes())

    def test_non_ascii_content_is_written_as_utf8(self):
        path = self.write(self.dir / "cordis.patch.yml", "name: old\n")
        write_text_atomic(path, "name: 图像理解\n")
        self.assertEqual("name: 图像理解\n".encode("utf-8"), path.read_bytes())

    def test_returns_none(self):
        path = self.write(self.dir / "cordis.patch.yml", "a: 1\n")
        self.assertIsNone(write_text_atomic(path, "a: 2\n"))


if __name__ == "__main__":
    unittest.main()
