"""Synchronization-semantics tests for dsh-byok-sync.

The whole module is offline: every HTTP call goes through :class:`FakeFetch`
(returning a :class:`~dsh_byok_sync.models.FetchResult`) and every credential
lookup goes through :class:`NoCredentials`, so no real network request is made
and no real credential file is read.

Covers SPEC.md §5 (non-text filter), §6 (sync semantics), §7 (CLI and report)
and §8 (test requirements). YAML fixtures are generated into workspace-local
temporary directories via :mod:`tests._helpers`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import io
import json
import os
import sys
import unittest
from unittest.mock import patch

try:  # `python -m unittest discover -s tests`
    import _helpers
except ImportError:  # `python -m unittest tests.test_sync`
    from tests import _helpers

from dsh_byok_sync import (
    __version__,
    cli,
    configfile,
    presets as presets_module,
    sync as sync_module,
    yamlpatch,
)
from dsh_byok_sync.models import (
    FetchResult,
    ModelSyncError,
    RemoteModel,
    base_display_name,
    is_non_text,
    is_non_text_model,
)

#: Environment variables that change what the tool reads or how it renders.
_ENV_KEYS = (
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "DSH_HOME",
    "DSH_PROFILE_HOME",
    "DSH_PROFILE",
    "DSH_PROFILE_DIR",
    "NO_COLOR",
)


@contextlib.contextmanager
def clean_env(**values):
    """Run a block with only the variables in *values* present."""
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


# --- fixtures --------------------------------------------------------------


def _render_models(entries, indent: str) -> str | None:
    """Render a ``models`` block; ``None`` omits the key entirely."""
    if entries is None:
        return None
    if not entries:
        return f"{indent}models: []"
    lines = [f"{indent}models:"]
    for entry in entries:
        if isinstance(entry, dict):
            first = True
            for key, value in entry.items():
                prefix = f"{indent}  - " if first else f"{indent}    "
                lines.append(f"{prefix}{key}: {value}")
                first = False
            if first:
                lines.append(f"{indent}  - {{}}")
        else:
            lines.append(f"{indent}  - {entry}")
    return "\n".join(lines)


def _render_route(name: str, spec: dict, indent: str) -> list[str]:
    lines = [f"{indent}{name}:"]
    field_indent = indent + "  "
    model_indent = indent + "  "
    for key, value in spec.items():
        if key == "models":
            block = _render_models(value, model_indent)
            if block is not None:
                lines.append(block)
        elif isinstance(value, dict):
            lines.append(f"{field_indent}{key}:")
            for sub_key, sub_value in value.items():
                lines.append(f"{field_indent}  {sub_key}: {sub_value}")
        else:
            lines.append(f"{field_indent}{key}: {value}")
    return lines


def build_patch(routes: dict, entry: str = "llm-pi-ai") -> str:
    """Render a ``cordis.patch.yml`` fixture (list root)."""
    lines = [
        f"- id: {entry}",
        '  name: "@deepseek-ai/dsh-llm-pi-ai"',
        "  config:",
        "    providers:",
    ]
    for name, spec in routes.items():
        lines.extend(_render_route(name, spec, "      "))
    return "\n".join(lines) + "\n"


def build_settings(routes: dict, entry: str = "llm-pi-ai") -> str:
    """Render a legacy ``settings.yaml`` fixture (mapping root)."""
    lines = [f"{entry}:", "  providers:"]
    for name, spec in routes.items():
        lines.extend(_render_route(name, spec, "    "))
    return "\n".join(lines) + "\n"


def route(models=None, base_url="https://x.test/v1", api="openai-completions", **extra) -> dict:
    """One provider route spec; ``models=None`` omits the ``models`` key."""
    spec: dict = {}
    if api is not None:
        spec["api"] = api
    if base_url is not None:
        spec["baseURL"] = base_url
    spec.update(extra)
    spec["models"] = models
    return spec


def patch_with_models(block: str, entry: str = "llm-pi-ai") -> str:
    """A patch file whose ``iris`` route ends with the given ``models`` snippet."""
    return (
        f"- id: {entry}\n"
        "  config:\n"
        "    providers:\n"
        "      iris:\n"
        "        api: openai-completions\n"
        "        baseURL: https://x.test/v1\n" + block
    )


class FakeFetch:
    """Stand-in for :func:`dsh_byok_sync.client.fetch_models`.

    ``table`` maps a URL (or any distinctive substring of one) to either a list
    of ids / :class:`RemoteModel` / mappings, an error string, or a ready-made
    :class:`FetchResult`. No socket is ever opened.
    """

    def __init__(self, table: dict):
        self.table = dict(table)
        self.calls: list[tuple] = []

    def _url_of(self, args, kwargs) -> str:
        for value in args:
            if isinstance(value, str) and "://" in value:
                return value
        for key in ("url", "models_url"):
            value = kwargs.get(key)
            if isinstance(value, str):
                return value
        raise AssertionError(f"fetch was called without a url: {args!r} {kwargs!r}")

    def _fixture(self, url: str):
        if url in self.table:
            return self.table[url]
        for key, value in self.table.items():
            if key in url:
                return value
        raise AssertionError(f"no fixture for {url}; fixtures: {sorted(self.table)}")

    def __call__(self, *args, **kwargs) -> FetchResult:
        url = self._url_of(args, kwargs)
        self.calls.append((url, args, kwargs))
        entry = self._fixture(url)
        if isinstance(entry, FetchResult):
            return entry
        if isinstance(entry, str):
            return FetchResult(url=url, models=(), error=entry)
        models: list[RemoteModel] = []
        for item in entry:
            if isinstance(item, RemoteModel):
                models.append(item)
            elif isinstance(item, dict):
                models.append(RemoteModel(**item))
            else:
                models.append(RemoteModel(id=str(item)))
        return FetchResult(url=url, models=tuple(models))


class NoCredentials:
    """Credential resolver that never touches the filesystem."""

    def __init__(self):
        self.asked: list[tuple] = []

    def resolve(self, api_key_env, route):
        self.asked.append((api_key_env, route))
        return None


# --- shared case -----------------------------------------------------------


class SyncCase(unittest.TestCase):
    """Temp-directory plumbing plus document/outcome helpers."""

    def setUp(self):
        self._dirs: list = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for directory in self._dirs:
            _helpers.cleanup(directory)

    def tmpdir(self):
        directory = _helpers.make_tmpdir()
        self._dirs.append(directory)
        return directory

    def document_path(self, text: str, name: str = "cordis.patch.yml"):
        path = self.tmpdir() / name
        path.write_bytes(text.encode("utf-8"))
        return path

    def document(self, text: str, name: str = "cordis.patch.yml", entry: str = "llm-pi-ai"):
        return configfile.load_document(self.document_path(text, name), entry)

    def outcome(self, document, fetch, routes=None, **kwargs):
        selected = list(document.routes) if routes is None else list(routes)
        kwargs.setdefault("credentials", NoCredentials())
        return sync_module.sync_config(document, selected, fetch=fetch, **kwargs)

    def rewritten(self, document, outcome):
        """Persist ``outcome.new_text`` into a temporary file and reparse it."""
        if outcome.new_text is None:
            return document
        path = self.tmpdir() / document.path.name
        path.write_bytes(outcome.new_text.encode("utf-8"))
        return configfile.load_document(path, document.entry_id)

    @staticmethod
    def ids(document, route_name: str) -> list[str]:
        entries = document.routes[route_name].models
        return [
            entry["id"]
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        ]

    @staticmethod
    def entry_of(document, route_name: str, model_id: str) -> dict:
        for entry in document.routes[route_name].models:
            if isinstance(entry, dict) and entry.get("id") == model_id:
                return entry
        raise AssertionError(f"{model_id!r} not present under {route_name!r}")

    @staticmethod
    def report(outcome, route_name: str):
        for report in outcome.reports:
            if report.route == route_name:
                return report
        raise AssertionError(
            f"no report for {route_name!r}; got {[r.route for r in outcome.reports]}"
        )


# --- §5 non-text filter ----------------------------------------------------


class NonTextFilterTest(unittest.TestCase):
    """SPEC §5 marker tables, including the documented false positives."""

    def test_short_roots_do_not_fire_inside_ordinary_words(self):
        # `voice` in invoice, `dall` in medallion, `sora` in sorami,
        # `imagen` in imagenet, `music` in musical, `image` in imagery,
        # `embed` in embedded, `diffusion` glued into diffusiongemma.
        for model_id in (
            "invoice-parser",
            "medallion-7b",
            "sorami-7b",
            "imagenet-classifier",
            "musical-theory-llm",
            "imagery-chat",
            "embedded-reasoning",
            "diffusiongemma-26b-a4b-it",
            "google/diffusiongemma-26b-a4b-it",
        ):
            with self.subTest(model_id=model_id):
                self.assertFalse(is_non_text_model(model_id), model_id)

    def test_media_models_are_filtered(self):
        for model_id in (
            "tts1",
            "asr1",
            "stt2",
            "sora2",
            "sora-2",
            "imagen4",
            "imagen-4",
            "dalle3",
            "dall-e-3",
            "diffusion3",
            "stable-diffusion-xl",
            "gpt-image-1",
            "text-embedding-3-large",
            "bge-embeddings",
            "bge-reranker-v2",
            "re-rank-pro",
            "whisper-large-v3",
            "speech-t5",
            "midjourney-v7",
            "qvq-audio-72b",
            "video-gen",
            "music-gen",
            "voice-chat",
        ):
            with self.subTest(model_id=model_id):
                self.assertTrue(is_non_text_model(model_id), model_id)

    def test_a_letter_continuation_blocks_a_token_match(self):
        for model_id in ("matt", "stts", "asrock", "ttsx", "xtts", "imagex"):
            with self.subTest(model_id=model_id):
                self.assertFalse(is_non_text_model(model_id), model_id)

    def test_matching_is_case_insensitive(self):
        for text in ("TTS-1", "Whisper-Turbo", "GPT-Image-1", "MIDJOURNEY-V7"):
            with self.subTest(text=text):
                self.assertTrue(is_non_text(text), text)

    def test_chinese_media_roots_are_filtered(self):
        for text in ("视频生成器", "图像生成", "图片模型", "音频转写", "语音合成", "音乐生成"):
            with self.subTest(text=text):
                self.assertTrue(is_non_text(text), text)

    def test_chinese_understanding_names_are_exempt(self):
        # 图像理解 / 视频理解 name vision-language *chat* models.
        for name in ("图像理解", "视频理解", "图片理解"):
            with self.subTest(name=name):
                self.assertFalse(is_non_text_model("some-model", name), name)
        for name in ("图像生成器", "语音合成"):
            with self.subTest(name=name):
                self.assertTrue(is_non_text_model("some-model", name), name)


# --- §6 additions and retention -------------------------------------------


class SyncAddTest(SyncCase):
    def test_new_models_are_appended_in_remote_order(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "old-model", "name": "Old"}])})
        )
        fetch = FakeFetch({"https://x.test/v1/models": ["old-model", "new-a", "new-b"]})
        outcome = self.outcome(document, fetch)

        report = self.report(outcome, "iris")
        self.assertEqual(("new-a", "new-b"), report.added)
        self.assertEqual(("old-model",), tuple(self.ids(document, "iris")))
        self.assertEqual(
            ["old-model", "new-a", "new-b"],
            self.ids(self.rewritten(document, outcome), "iris"),
        )
        self.assertTrue(report.changed)
        self.assertIsNotNone(outcome.new_text)

    def test_new_model_keeps_remote_metadata(self):
        document = self.document(build_patch({"iris": route(models=[{"id": "old"}])}))
        remote = RemoteModel(
            id="glm-5.3-flash", name="GLM 5.3 Flash", contextWindow=200000, maxTokens=64000
        )
        outcome = self.outcome(document, FakeFetch({"x.test": [remote]}))

        entry = self.entry_of(self.rewritten(document, outcome), "iris", "glm-5.3-flash")
        self.assertEqual("GLM 5.3 Flash", entry["name"])
        self.assertEqual(200000, entry["contextWindow"])
        self.assertEqual(64000, entry["maxTokens"])

    def test_new_model_without_metadata_omits_optional_fields(self):
        document = self.document(build_patch({"iris": route(models=[{"id": "old"}])}))
        outcome = self.outcome(document, FakeFetch({"x.test": ["mystery-1"]}))

        entry = self.entry_of(self.rewritten(document, outcome), "iris", "mystery-1")
        self.assertEqual({"id", "name"}, set(entry))

    def test_new_model_name_falls_back_to_the_id(self):
        document = self.document(build_patch({"iris": route(models=[{"id": "old"}])}))
        outcome = self.outcome(document, FakeFetch({"x.test": ["agnes-2.0-flash"]}))

        entry = self.entry_of(self.rewritten(document, outcome), "iris", "agnes-2.0-flash")
        self.assertEqual("agnes-2.0-flash", entry["name"])
        self.assertEqual(
            base_display_name(RemoteModel(id="agnes-2.0-flash")), entry["name"]
        )

    def test_new_model_keeps_remote_display_name_verbatim(self):
        document = self.document(build_patch({"iris": route(models=[{"id": "old"}])}))
        remote = RemoteModel(id="agnes-2.0-flash", name="Agnes 2.0 Flash")
        outcome = self.outcome(document, FakeFetch({"x.test": [remote]}))

        entry = self.entry_of(self.rewritten(document, outcome), "iris", "agnes-2.0-flash")
        self.assertEqual("Agnes 2.0 Flash", entry["name"])

    def test_existing_entries_keep_their_fields(self):
        document = self.document(
            build_patch(
                {
                    "iris": route(
                        models=[
                            {
                                "id": "keep-me",
                                "name": "Keep Me",
                                "contextWindow": 512000,
                                "input": "[text, image]",
                            }
                        ]
                    )
                }
            )
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["keep-me", "brand-new"]}))

        entry = self.entry_of(self.rewritten(document, outcome), "iris", "keep-me")
        self.assertEqual("Keep Me", entry["name"])
        self.assertEqual(512000, entry["contextWindow"])
        modalities = entry["input"]
        if isinstance(modalities, str):
            modalities = [part.strip() for part in modalities.strip("[]").split(",")]
        self.assertEqual(["text", "image"], list(modalities))

    def test_comment_above_a_kept_entry_survives(self):
        text = (
            "- id: llm-pi-ai\n"
            "  config:\n"
            "    providers:\n"
            "      iris:\n"
            "        api: openai-completions\n"
            "        baseURL: https://x.test/v1\n"
            "        models:\n"
            "          # hand-authored entry\n"
            "          - id: keep-me\n"
            "            name: Keep Me\n"
        )
        document = self.document(text)
        outcome = self.outcome(document, FakeFetch({"x.test": ["keep-me", "new-one"]}))

        self.assertIn("# hand-authored entry", outcome.new_text)
        self.assertIn("new-one", outcome.new_text)

    def test_models_block_is_created_when_absent(self):
        document = self.document(build_patch({"iris": route(models=None)}))
        outcome = self.outcome(document, FakeFetch({"x.test": ["fresh"]}))

        self.assertEqual(["fresh"], self.ids(self.rewritten(document, outcome), "iris"))

    def test_null_models_key_is_filled_as_a_block_sequence(self):
        # A bare ``models:`` is allowed to become a block list (SPEC §10 only
        # refuses values whose bytes cannot be preserved).
        document = self.document(patch_with_models("        models:\n"))
        outcome = self.outcome(document, FakeFetch({"x.test": ["fresh"]}))

        self.assertEqual(["fresh"], self.ids(self.rewritten(document, outcome), "iris"))

    def test_settings_shaped_document_roundtrips(self):
        document = self.document(
            build_settings({"iris": route(models=[{"id": "old"}])}), name="settings.yaml"
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["old", "new"]}))

        reparsed = self.rewritten(document, outcome)
        self.assertEqual("settings", reparsed.kind)
        self.assertEqual(["old", "new"], self.ids(reparsed, "iris"))


class SyncNoOpTest(SyncCase):
    def test_noop_returns_no_new_text(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "a"}, {"id": "b"}])})
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["a", "b"]}))

        self.assertIsNone(outcome.new_text)
        report = self.report(outcome, "iris")
        self.assertFalse(report.changed)
        self.assertEqual(2, report.kept)
        self.assertEqual((), report.added)
        self.assertEqual((), report.removed)


class NoOpPlannerTest(SyncCase):
    """SPEC §6.9/§10: an identity plan never reaches the rewriter."""

    def test_identity_plan_does_not_call_apply_plan(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "a"}, {"id": "b"}])})
        )
        with patch("dsh_byok_sync.sync.yamlpatch.apply_plan") as apply_plan:
            outcome = self.outcome(document, FakeFetch({"x.test": ["a", "b"]}))

        apply_plan.assert_not_called()
        self.assertIsNone(outcome.new_text)
        self.assertFalse(self.report(outcome, "iris").changed)

    def test_empty_flow_models_passes_read_only(self):
        # Nothing to keep and nothing text to add: the route is not rewritten,
        # which is what lets an ``models: []`` route survive a failed sync.
        document = self.document(patch_with_models("        models: []\n"))
        with patch("dsh_byok_sync.sync.yamlpatch.apply_plan") as apply_plan:
            outcome = self.outcome(document, FakeFetch({"x.test": ["sora2"]}))

        apply_plan.assert_not_called()
        self.assertIsNone(outcome.new_text)
        self.assertEqual(("sora2",), self.report(outcome, "iris").filtered)

    def test_a_real_change_does_call_apply_plan(self):
        # Guard for the guard: the patch really does intercept the rewriter.
        document = self.document(build_patch({"iris": route(models=[{"id": "a"}])}))
        with patch(
            "dsh_byok_sync.sync.yamlpatch.apply_plan", wraps=yamlpatch.apply_plan
        ) as apply_plan:
            outcome = self.outcome(document, FakeFetch({"x.test": ["a", "b"]}))

        self.assertTrue(apply_plan.called)
        self.assertIsNotNone(outcome.new_text)


class UnsafeModelsTest(SyncCase):
    """SPEC §10: a ``models`` shape whose bytes cannot be preserved is refused.

    The route must report an error, never place a model, never reach the
    network, and never write the file (a rewrite would corrupt the original).
    """

    SHAPES = {
        "flow": "        models: [alpha, beta]\n",
        "mapping": "        models:\n          alpha:\n            name: Alpha\n",
        "scalar": "        models: not-a-list\n",
        "block scalar": "        models: |\n          - id: alpha\n",
    }

    def refused_outcome(self, shape: str):
        document = self.document(patch_with_models(self.SHAPES[shape]))
        fetch = FakeFetch({"x.test": ["alpha", "beta"]})
        return document, fetch, self.outcome(document, fetch)

    def assert_refused(self, shape: str) -> None:
        _document, fetch, outcome = self.refused_outcome(shape)
        report = self.report(outcome, "iris")
        self.assertTrue(report.error, f"{shape}: expected an error report")
        self.assertFalse(report.changed, f"{shape}: must not report a change")
        self.assertEqual((), report.added, shape)
        self.assertEqual((), report.removed, shape)
        self.assertIsNone(outcome.new_text, f"{shape}: must not rewrite the file")
        self.assertEqual([], fetch.calls, f"{shape}: must not reach the endpoint")

    def test_flow_style_models_is_refused(self):
        self.assert_refused("flow")

    def test_models_mapping_value_is_refused(self):
        self.assert_refused("mapping")

    def test_models_scalar_value_is_refused(self):
        self.assert_refused("scalar")

    def test_models_block_scalar_is_refused(self):
        self.assert_refused("block scalar")

    def test_flow_style_error_names_the_shape(self):
        _document, _fetch, outcome = self.refused_outcome("flow")
        self.assertIn("流风格", self.report(outcome, "iris").error)


# --- §6 deletions and the authorization gate ------------------------------


class SyncDeleteTest(SyncCase):
    def test_stale_model_is_removed(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "old"}, {"id": "stale"}])})
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["old"]}))

        report = self.report(outcome, "iris")
        self.assertEqual(("stale",), report.removed)
        self.assertEqual(["old"], self.ids(self.rewritten(document, outcome), "iris"))

    def test_no_delete_keeps_stale_models(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "old"}, {"id": "stale"}])})
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["old"]}), no_delete=True)

        report = self.report(outcome, "iris")
        self.assertEqual((), report.removed)
        self.assertEqual(2, report.kept)
        self.assertIsNotNone(report.deletion_skipped)
        self.assertIn("no-delete", report.deletion_skipped)
        self.assertIsNone(outcome.new_text)

    def test_failed_request_blocks_deletion(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "a"}, {"id": "b"}])})
        )
        outcome = self.outcome(document, FakeFetch({"x.test": "HTTP 401 Unauthorized"}))

        report = self.report(outcome, "iris")
        self.assertEqual("HTTP 401 Unauthorized", report.error)
        self.assertEqual((), report.removed)
        self.assertEqual((), report.added)
        self.assertEqual(2, report.kept)
        self.assertIsNone(outcome.new_text)

    def test_failed_request_writes_nothing_even_with_flags(self):
        """With no remote data ``--sort``/``--no-delete`` must not rewrite a file."""
        document = self.document(
            build_patch({"iris": route(models=[{"id": "b"}, {"id": "a"}])})
        )
        outcome = self.outcome(
            document, FakeFetch({"x.test": "HTTP 500"}), no_delete=True, sort=True
        )

        self.assertEqual("HTTP 500", self.report(outcome, "iris").error)
        self.assertFalse(self.report(outcome, "iris").changed)
        self.assertIsNone(outcome.new_text)

    def test_response_without_text_models_skips_deletion(self):
        document = self.document(build_patch({"iris": route(models=[{"id": "old-chat"}])}))
        outcome = self.outcome(document, FakeFetch({"x.test": ["sora2", "tts1"]}))

        report = self.report(outcome, "iris")
        self.assertTrue(report.no_text_signal)
        self.assertEqual(("sora2", "tts1"), report.filtered)
        self.assertEqual((), report.removed)
        self.assertEqual(1, report.kept)
        self.assertIsNone(outcome.new_text)

    def test_a_text_model_authorises_deletion(self):
        document = self.document(build_patch({"iris": route(models=[{"id": "old-chat"}])}))
        outcome = self.outcome(document, FakeFetch({"x.test": ["tts1", "chat-a"]}))

        report = self.report(outcome, "iris")
        self.assertFalse(report.no_text_signal)
        self.assertEqual(("tts1",), report.filtered)
        self.assertEqual(("old-chat",), report.removed)
        self.assertEqual(("chat-a",), report.added)

    def test_no_text_signal_is_false_when_nothing_is_stale(self):
        """The flag means "deletion was skipped for lack of a text signal",
        not "this endpoint only advertises media models"."""
        document = self.document(build_patch({"iris": route(models=[{"id": "sora2"}])}))
        outcome = self.outcome(document, FakeFetch({"x.test": ["sora2"]}))

        report = self.report(outcome, "iris")
        self.assertFalse(report.no_text_signal)
        self.assertEqual(1, report.kept)
        self.assertIsNone(report.deletion_skipped)
        self.assertIsNone(outcome.new_text)

    def test_route_without_base_url_is_not_synced(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "keep"}], base_url=None)})
        )
        fetch = FakeFetch({})
        outcome = self.outcome(document, fetch)

        report = self.report(outcome, "iris")
        self.assertIsNone(report.url)
        self.assertEqual("no endpoint url", report.error)
        self.assertEqual(1, report.kept)
        self.assertEqual((), report.added)
        self.assertEqual((), report.removed)
        self.assertIsNone(outcome.new_text)
        self.assertEqual([], fetch.calls)

    def test_blank_base_url_is_treated_as_missing(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "keep"}], base_url='""')})
        )
        outcome = self.outcome(document, FakeFetch({}))

        self.assertEqual("no endpoint url", self.report(outcome, "iris").error)
        self.assertIsNone(outcome.new_text)

    def test_one_failed_route_does_not_block_the_others(self):
        text = build_patch(
            {
                "iris": route(models=[{"id": "iris-old"}]),
                "nvidia": route(models=[{"id": "nv-old"}, {"id": "nv-stale"}], base_url="https://n.test/v1"),
            }
        )
        document = self.document(text)
        fetch = FakeFetch({"x.test": "HTTP 401", "https://n.test/v1/models": ["nv-old"]})
        outcome = self.outcome(document, fetch)

        self.assertEqual("HTTP 401", self.report(outcome, "iris").error)
        self.assertEqual((), self.report(outcome, "iris").removed)
        self.assertEqual(("nv-stale",), self.report(outcome, "nvidia").removed)
        reparsed = self.rewritten(document, outcome)
        self.assertEqual(["iris-old"], self.ids(reparsed, "iris"))
        self.assertEqual(["nv-old"], self.ids(reparsed, "nvidia"))


# --- §5/§6 interaction: the filter never deletes local models -------------


class SyncNonTextTest(SyncCase):
    FILTERED = ("tts1", "asr1", "sora2", "imagen4", "dalle3", "diffusion3")
    SURVIVING = (
        "invoice-parser",
        "medallion-7b",
        "sorami-7b",
        "imagenet-classifier",
        "musical-theory-llm",
        "imagery-chat",
        "embedded-reasoning",
        "diffusiongemma-26b-a4b-it",
    )

    def test_non_text_models_are_never_added(self):
        document = self.document(build_patch({"iris": route(models=[{"id": "chat-a"}])}))
        outcome = self.outcome(
            document, FakeFetch({"x.test": ["chat-a", *self.FILTERED, *self.SURVIVING]})
        )

        report = self.report(outcome, "iris")
        self.assertEqual(self.FILTERED, report.filtered)
        self.assertEqual(self.SURVIVING, report.added)
        self.assertEqual(
            ["chat-a", *self.SURVIVING], self.ids(self.rewritten(document, outcome), "iris")
        )

    def test_id_with_only_non_text_entries_is_filtered(self):
        document = self.document(build_patch({"iris": route(models=[{"id": "chat-a"}])}))
        remote = [
            RemoteModel(id="chat-a"),
            RemoteModel(id="sora2", name="Sora 2"),
            RemoteModel(id="sora2", name="视频生成"),
        ]
        outcome = self.outcome(document, FakeFetch({"x.test": remote}))

        report = self.report(outcome, "iris")
        self.assertEqual(("sora2",), report.filtered)
        self.assertEqual((), report.added)

    def test_duplicate_id_with_one_text_entry_counts_as_text(self):
        document = self.document(build_patch({"iris": route(models=[{"id": "chat-a"}])}))
        remote = [
            RemoteModel(id="chat-a"),
            RemoteModel(id="dup-x", name="图像生成"),
            RemoteModel(id="dup-x", name="friendly text chat"),
        ]
        outcome = self.outcome(document, FakeFetch({"x.test": remote}))

        report = self.report(outcome, "iris")
        self.assertEqual(("dup-x",), report.added)
        self.assertEqual((), report.filtered)

    def test_local_non_text_entry_survives_when_remote_still_lists_it(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "whisper-large-v3"}, {"id": "chat-a"}])})
        )
        outcome = self.outcome(
            document, FakeFetch({"x.test": ["whisper-large-v3", "chat-a"]})
        )

        report = self.report(outcome, "iris")
        self.assertEqual(2, report.kept)
        self.assertEqual((), report.removed)
        self.assertIsNone(outcome.new_text)

    def test_local_non_text_entry_is_removed_when_remote_drops_it(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "whisper-large-v3"}, {"id": "chat-a"}])})
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["chat-a"]}))

        self.assertEqual(("whisper-large-v3",), self.report(outcome, "iris").removed)

    def test_remote_display_name_participates_in_the_filter(self):
        """SPEC §5 filters on ``id`` *and* ``name``."""
        document = self.document(build_patch({"iris": route(models=[{"id": "chat-a"}])}))
        remote = [
            RemoteModel(id="chat-a"),
            RemoteModel(id="mystery-v1", name="Whisper Turbo"),
        ]
        outcome = self.outcome(document, FakeFetch({"x.test": remote}))

        self.assertEqual(("mystery-v1",), self.report(outcome, "iris").filtered)

    def test_remote_chinese_understanding_name_is_not_filtered(self):
        document = self.document(build_patch({"iris": route(models=[{"id": "chat-a"}])}))
        remote = [
            RemoteModel(id="chat-a"),
            RemoteModel(id="vision-chat", name="图像理解"),
        ]
        outcome = self.outcome(document, FakeFetch({"x.test": remote}))

        self.assertEqual(("vision-chat",), self.report(outcome, "iris").added)


# --- §6.7 invalid local entries -------------------------------------------


class SyncInvalidEntriesTest(SyncCase):
    def test_entry_without_id_is_discarded(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "ok"}, {"name": "broken"}])})
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["ok"]}))

        report = self.report(outcome, "iris")
        self.assertEqual(1, len(report.discarded))
        self.assertTrue(report.changed)
        self.assertIsNotNone(outcome.new_text)
        self.assertEqual(["ok"], self.ids(self.rewritten(document, outcome), "iris"))

    def test_non_mapping_entry_is_discarded(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "ok"}, "scalar-entry"])})
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["ok"]}))

        report = self.report(outcome, "iris")
        self.assertEqual(1, len(report.discarded))
        self.assertEqual(["ok"], self.ids(self.rewritten(document, outcome), "iris"))

    def test_discard_happens_in_no_delete_mode(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "ok"}, {"name": "broken"}])})
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["ok"]}), no_delete=True)

        report = self.report(outcome, "iris")
        self.assertEqual(1, len(report.discarded))
        self.assertIsNotNone(outcome.new_text)

    def test_discard_happens_after_a_failed_request(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "ok"}, {"name": "broken"}])})
        )
        outcome = self.outcome(document, FakeFetch({"x.test": "HTTP 500"}))

        report = self.report(outcome, "iris")
        self.assertEqual("HTTP 500", report.error)
        self.assertEqual(1, len(report.discarded))
        self.assertIsNotNone(outcome.new_text)


# --- §6.8 ordering --------------------------------------------------------


class SyncOrderTest(SyncCase):
    def test_default_order_is_preserved_and_new_models_appended(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "b"}, {"id": "a"}])})
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["a", "b", "c"]}))

        self.assertEqual(
            ["b", "a", "c"], self.ids(self.rewritten(document, outcome), "iris")
        )

    def test_sort_orders_models_by_id(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "b"}, {"id": "a"}])})
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["a", "b", "c"]}), sort=True)

        self.assertEqual(
            ["a", "b", "c"], self.ids(self.rewritten(document, outcome), "iris")
        )

    def test_sort_is_case_sensitive(self):
        document = self.document(
            build_patch(
                {"iris": route(models=[{"id": "Zeta"}, {"id": "alpha"}, {"id": "Beta"}])}
            )
        )
        outcome = self.outcome(
            document, FakeFetch({"x.test": ["Zeta", "alpha", "Beta"]}), sort=True
        )

        self.assertEqual(
            ["Beta", "Zeta", "alpha"],
            self.ids(self.rewritten(document, outcome), "iris"),
        )

    def test_sort_leaves_a_sorted_list_untouched(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "a"}, {"id": "b"}])})
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["a", "b"]}), sort=True)

        self.assertIsNone(outcome.new_text)


# --- §6.10 duplicate ids --------------------------------------------------


class SyncDuplicateIdTest(SyncCase):
    def test_duplicate_local_ids_survive_without_delete(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "dup"}, {"id": "dup"}])})
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["dup"]}), no_delete=True)

        report = self.report(outcome, "iris")
        self.assertEqual(2, report.kept)
        self.assertIsNone(outcome.new_text)

    def test_duplicate_local_ids_kept_when_remote_still_lists_the_id(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "dup"}, {"id": "dup"}])})
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["dup"]}))

        report = self.report(outcome, "iris")
        self.assertEqual(2, report.kept)
        self.assertEqual((), report.removed)
        self.assertEqual(2, len(self.ids(self.rewritten(document, outcome), "iris")))

    def test_duplicate_local_ids_all_removed_when_remote_forgets_them(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "dup"}, {"id": "dup"}])})
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["other-chat"]}))

        report = self.report(outcome, "iris")
        self.assertEqual(("dup", "dup"), report.removed)
        self.assertEqual(["other-chat"], self.ids(self.rewritten(document, outcome), "iris"))


# --- §3.6 report dataclass ------------------------------------------------


class SyncReportTest(SyncCase):
    def test_route_report_exposes_the_documented_fields(self):
        documented = {
            "route",
            "url",
            "error",
            "added",
            "removed",
            "kept",
            "filtered",
            "no_text_signal",
            "deletion_skipped",
            "discarded",
            "credits",
            "changed",
        }
        fields = {field.name for field in dataclasses.fields(sync_module.RouteReport)}
        self.assertTrue(documented.issubset(fields), documented - fields)

    def test_unselected_routes_are_left_alone(self):
        document = self.document(
            build_patch(
                {
                    "iris": route(models=[{"id": "iris-a"}]),
                    "nvidia": route(
                        models=[{"id": "nv-a"}, {"id": "nv-stale"}], base_url="https://n.test/v1"
                    ),
                }
            )
        )
        outcome = self.outcome(
            document, FakeFetch({"https://x.test/v1/models": ["iris-a", "iris-b"]}), routes=["iris"]
        )

        self.assertEqual(1, len(outcome.reports))
        reparsed = self.rewritten(document, outcome)
        self.assertEqual(["iris-a", "iris-b"], self.ids(reparsed, "iris"))
        self.assertEqual(["nv-a", "nv-stale"], self.ids(reparsed, "nvidia"))

    def test_reports_follow_the_selection_order(self):
        document = self.document(
            build_patch(
                {
                    "iris": route(models=[{"id": "i"}]),
                    "nvidia": route(models=[{"id": "n"}], base_url="https://n.test/v1"),
                }
            )
        )
        fetch = FakeFetch({"https://n.test/v1/models": ["n"], "https://x.test/v1/models": ["i"]})
        outcome = self.outcome(document, fetch, routes=["nvidia", "iris"])

        self.assertEqual(["nvidia", "iris"], [report.route for report in outcome.reports])

    def test_unknown_route_raises_model_sync_error(self):
        document = self.document(build_patch({"iris": route(models=[{"id": "a"}])}))
        with self.assertRaises(ModelSyncError):
            self.outcome(document, FakeFetch({"x.test": ["a"]}), routes=["nope"])

    def test_credits_are_attached_for_known_hosts(self):
        document = self.document(build_patch({"iris": route(models=[{"id": "a"}])}))
        calls: list = []
        with patch.object(sync_module, "known_credit_host", return_value="test-host"):
            outcome = self.outcome(
                document,
                FakeFetch({"x.test": ["a"]}),
                credits_fetch=lambda *args: calls.append(args) or "CNY 12.34",
            )

        self.assertEqual("CNY 12.34", self.report(outcome, "iris").credits)
        self.assertEqual(1, len(calls))

    def test_credits_can_be_disabled(self):
        document = self.document(build_patch({"iris": route(models=[{"id": "a"}])}))
        calls: list = []
        with patch.object(sync_module, "known_credit_host", return_value="test-host"):
            outcome = self.outcome(
                document,
                FakeFetch({"x.test": ["a"]}),
                use_credits=False,
                credits_fetch=lambda *args: calls.append(args) or "CNY 12.34",
            )

        self.assertIsNone(self.report(outcome, "iris").credits)
        self.assertEqual([], calls)

    def test_auth_env_of_the_route_is_resolved(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "a"}], apiKeyEnv="IRIS_API_KEY")})
        )
        credentials = NoCredentials()
        self.outcome(document, FakeFetch({"x.test": ["a"]}), credentials=credentials)

        self.assertEqual([("IRIS_API_KEY", "iris")], credentials.asked)


# --- presets --------------------------------------------------------------


class PresetTest(SyncCase):
    def write_presets(self, payload) -> "os.PathLike":
        path = self.tmpdir() / "presets.json"
        path.write_bytes(json.dumps(payload).encode("utf-8"))
        return path

    def sync_single(self, model_id: str):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "old-chat"}], base_url="https://x.test/v1")})
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["old-chat", model_id]}))
        return self.entry_of(self.rewritten(document, outcome), "iris", model_id)

    def test_new_deepseek_model_receives_the_builtin_preset(self):
        entry = self.sync_single("deepseek-v4.1-flash")
        self.assertEqual(1000000, entry["contextWindow"])
        self.assertEqual(384000, entry["maxTokens"])
        self.assertIn("off", entry["reasoningEfforts"])
        self.assertIn("high", entry["reasoningEfforts"])

    def test_remote_metadata_beats_the_preset(self):
        document = self.document(
            build_patch({"iris": route(models=[{"id": "old-chat"}])})
        )
        remote = RemoteModel(
            id="deepseek-v4.1-flash", contextWindow=64000, maxTokens=8192
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["old-chat", remote]}))

        entry = self.entry_of(self.rewritten(document, outcome), "iris", "deepseek-v4.1-flash")
        self.assertEqual(64000, entry["contextWindow"])
        self.assertEqual(8192, entry["maxTokens"])

    def test_existing_deepseek_model_is_not_represet(self):
        document = self.document(
            build_patch(
                {
                    "iris": route(
                        models=[
                            {
                                "id": "deepseek-v4-flash",
                                "name": "deepseek-v4-flash",
                                "contextWindow": 512000,
                                "maxTokens": 128000,
                            }
                        ]
                    )
                }
            )
        )
        outcome = self.outcome(document, FakeFetch({"x.test": ["deepseek-v4-flash"]}))

        entry = self.entry_of(self.rewritten(document, outcome), "iris", "deepseek-v4-flash")
        self.assertEqual(512000, entry["contextWindow"])
        self.assertEqual(128000, entry["maxTokens"])
        self.assertNotIn("reasoningEfforts", entry)
        self.assertIsNone(outcome.new_text)

    def test_first_matching_preset_wins(self):
        path = self.write_presets(
            {
                "presets": [
                    {"model_id_contains": ["alpha"], "overrides": {"contextWindow": 1000}},
                    {"model_id_contains": ["alpha", "beta"], "overrides": {"contextWindow": 2000}},
                ]
            }
        )
        presets = presets_module.load_presets(path)
        self.assertEqual({"contextWindow": 1000}, presets_module.apply_presets("alpha-1", presets))
        self.assertEqual({"contextWindow": 2000}, presets_module.apply_presets("beta-1", presets))

    def test_forbidden_override_field_raises(self):
        path = self.write_presets(
            {"presets": [{"model_id_contains": ["x"], "overrides": {"vendor": "acme"}}]}
        )
        with self.assertRaises(ModelSyncError):
            presets_module.load_presets(path)

    def test_unknown_reasoning_level_raises(self):
        path = self.write_presets(
            {
                "presets": [
                    {
                        "model_id_contains": ["x"],
                        "overrides": {"reasoningEfforts": {"ultra": "ultra"}},
                    }
                ]
            }
        )
        with self.assertRaises(ModelSyncError):
            presets_module.load_presets(path)

    def test_unknown_modality_raises(self):
        path = self.write_presets(
            {"presets": [{"model_id_contains": ["x"], "overrides": {"input": ["audio"]}}]}
        )
        with self.assertRaises(ModelSyncError):
            presets_module.load_presets(path)

    def test_corrupt_presets_file_raises(self):
        path = self.tmpdir() / "presets.json"
        path.write_bytes(b"{not json")
        with self.assertRaises(ModelSyncError):
            presets_module.load_presets(path)

    def test_builtin_presets_load_and_match_deepseek(self):
        presets = presets_module.load_presets()
        self.assertTrue(presets)
        self.assertTrue(any(preset.matches("deepseek-v4.1-flash") for preset in presets))


# --- §7 CLI ---------------------------------------------------------------


class CliStream(io.StringIO):
    """A captured stream whose TTY-ness the test controls."""

    def __init__(self, tty: bool = False):
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:  # noqa: D102 - mirrors io.IOBase
        return self._tty


@contextlib.contextmanager
def fake_network(fetch, credits=None):
    """Patch every module-level fetch entry point; nothing reaches a socket."""
    with contextlib.ExitStack() as stack:
        for module_name in ("dsh_byok_sync.sync", "dsh_byok_sync.cli", "dsh_byok_sync.client"):
            try:
                module = importlib.import_module(module_name)
            except ImportError:  # pragma: no cover - defensive
                continue
            if hasattr(module, "fetch_models"):
                stack.enter_context(patch.object(module, "fetch_models", fetch))
            if credits is not None and hasattr(module, "fetch_credits"):
                stack.enter_context(patch.object(module, "fetch_credits", credits))
        yield


class CliCase(SyncCase):
    def setUp(self):
        super().setUp()
        self.empty_home = self.tmpdir()
        self.missing_credentials = self.tmpdir() / "no-such-credentials.yaml"

    def run_cli(self, argv, *, tty=False, env=None):
        argv = list(argv)
        if "--credentials" not in argv:
            argv += ["--credentials", str(self.missing_credentials)]
        values = {"DSH_HOME": self.empty_home, "HOME": self.empty_home, "USERPROFILE": self.empty_home}
        if env:
            values.update(env)
        out, err = CliStream(tty), CliStream(False)
        with clean_env(**values), patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
            code = self._invoke(argv)
        return code, out.getvalue(), err.getvalue()

    def _invoke(self, argv) -> int:
        entry = getattr(cli, "run", None) or cli.main
        try:
            result = entry(argv)
        except SystemExit as exc:
            return 0 if exc.code is None else int(exc.code)
        return 0 if result is None else int(result)

    def config_path(self, text: str = None, name: str = "cordis.patch.yml"):
        if text is None:
            text = build_patch({"iris": route(models=[{"id": "old-chat"}])})
        return self.document_path(text, name)


class CliArgsTest(CliCase):
    def test_version_exits_zero(self):
        code, out, _err = self.run_cli(["--version"])
        self.assertEqual(0, code)
        self.assertIn(__version__, out)
        self.assertIn("dsh-byok-sync", out)

    def test_all_and_provider_are_mutually_exclusive(self):
        path = self.config_path()
        with fake_network(FakeFetch({"x.test": ["old-chat"]})):
            code, _out, err = self.run_cli(
                ["--config", str(path), "--all", "--provider", "iris"]
            )
        self.assertEqual(2, code)
        self.assertIn("--provider", err)

    def test_selection_is_required(self):
        path = self.config_path()
        with fake_network(FakeFetch({"x.test": ["old-chat"]})):
            code, _out, err = self.run_cli(["--config", str(path)])
        self.assertEqual(2, code)
        self.assertIn("--all", err)

    def test_missing_config_exits_two(self):
        missing = self.empty_home / "nope.yml"
        with fake_network(FakeFetch({"x.test": []})):
            code, _out, _err = self.run_cli(["--config", str(missing), "--all"])
        self.assertEqual(2, code)

    def test_unknown_provider_exits_two(self):
        path = self.config_path()
        with fake_network(FakeFetch({"x.test": ["old-chat"]})):
            code, _out, _err = self.run_cli(["--config", str(path), "--provider", "nope"])
        self.assertEqual(2, code)

    def test_duplicate_provider_flag_produces_one_report(self):
        path = self.config_path()
        with fake_network(FakeFetch({"x.test": ["old-chat"]})):
            code, out, _err = self.run_cli(
                [
                    "--config",
                    str(path),
                    "--provider",
                    "iris",
                    "--provider",
                    "iris",
                    "--no-credits",
                ]
            )

        self.assertEqual(0, code)
        self.assertEqual(1, out.count("[unchanged] iris"))
        summary = next(line for line in out.splitlines() if line.startswith("Summary:"))
        self.assertTrue(summary.startswith("Summary: 1 routes"), summary)

    def test_list_routes_prints_routes_without_network(self):
        path = self.config_path(
            build_patch(
                {
                    "iris": route(models=[{"id": "i"}]),
                    "nvidia": route(models=[{"id": "n"}], base_url="https://n.test/v1"),
                }
            )
        )
        fetch = FakeFetch({})
        with fake_network(fetch):
            code, out, _err = self.run_cli(["--config", str(path), "--list-routes"])

        self.assertEqual(0, code)
        self.assertEqual(["iris", "nvidia"], [line.strip() for line in out.splitlines() if line.strip()])
        self.assertEqual([], fetch.calls)

    def test_custom_entry_is_selectable(self):
        path = self.config_path(
            build_patch({"routed": route(models=[{"id": "only-one"}])}, entry="other-plugin"),
            name="other.yml",
        )
        with fake_network(FakeFetch({"x.test": ["only-one"]})):
            ok, _out, _err = self.run_cli(
                ["--config", str(path), "--entry", "other-plugin", "--all", "--no-credits"]
            )
            missing, _out2, _err2 = self.run_cli(
                ["--config", str(path), "--all", "--no-credits"]
            )
        self.assertEqual(0, ok)
        self.assertEqual(2, missing)


class CliRunTest(CliCase):
    def test_successful_run_exits_zero_and_writes_the_file(self):
        path = self.config_path()
        with fake_network(FakeFetch({"x.test": ["old-chat", "new-chat"]})):
            code, out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(0, code)
        self.assertIn("Summary:", out)
        self.assertIn("added (", out)
        reparsed = configfile.load_document(path)
        self.assertEqual(["old-chat", "new-chat"], self.ids(reparsed, "iris"))

    def test_failed_endpoint_exits_one_and_keeps_the_file(self):
        path = self.config_path()
        before = path.read_bytes()
        with fake_network(FakeFetch({"x.test": "HTTP 401"})):
            code, out, err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(1, code)
        self.assertIn("HTTP 401", out + err)
        self.assertIn("error:", out)
        self.assertEqual(before, path.read_bytes())

    def test_dry_run_does_not_write(self):
        path = self.config_path()
        before = path.read_bytes()
        with fake_network(FakeFetch({"x.test": ["old-chat", "new-chat"]})):
            code, out, _err = self.run_cli(
                ["--config", str(path), "--all", "--dry-run", "--no-credits"]
            )

        self.assertEqual(0, code)
        self.assertIn("added (", out)
        self.assertEqual(before, path.read_bytes())

    def test_no_delete_flag_keeps_stale_models_on_disk(self):
        path = self.config_path(
            build_patch({"iris": route(models=[{"id": "old-chat"}, {"id": "stale"}])})
        )
        with fake_network(FakeFetch({"x.test": ["old-chat"]})):
            code, _out, _err = self.run_cli(
                ["--config", str(path), "--all", "--no-delete", "--no-credits"]
            )

        self.assertEqual(0, code)
        self.assertEqual(["old-chat", "stale"], self.ids(configfile.load_document(path), "iris"))

    def test_deletes_stale_models_by_default(self):
        path = self.config_path(
            build_patch({"iris": route(models=[{"id": "old-chat"}, {"id": "stale"}])})
        )
        with fake_network(FakeFetch({"x.test": ["old-chat"]})):
            code, _out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(0, code)
        self.assertEqual(["old-chat"], self.ids(configfile.load_document(path), "iris"))

    def test_provider_flag_only_touches_the_named_route(self):
        path = self.config_path(
            build_patch(
                {
                    "iris": route(models=[{"id": "iris-a"}]),
                    "nvidia": route(
                        models=[{"id": "nv-a"}, {"id": "nv-stale"}], base_url="https://n.test/v1"
                    ),
                }
            )
        )
        fetch = FakeFetch(
            {"https://x.test/v1/models": ["iris-a", "iris-b"], "https://n.test/v1/models": ["nv-a"]}
        )
        with fake_network(fetch):
            code, _out, _err = self.run_cli(
                ["--config", str(path), "--provider", "iris", "--no-credits"]
            )

        self.assertEqual(0, code)
        reparsed = configfile.load_document(path)
        self.assertEqual(["iris-a", "iris-b"], self.ids(reparsed, "iris"))
        self.assertEqual(["nv-a", "nv-stale"], self.ids(reparsed, "nvidia"))
        self.assertTrue(all("x.test" in url for url, _a, _k in fetch.calls))

    def test_sort_flag_orders_the_written_file(self):
        path = self.config_path(
            build_patch({"iris": route(models=[{"id": "b"}, {"id": "a"}])})
        )
        with fake_network(FakeFetch({"x.test": ["a", "b", "c"]})):
            code, _out, _err = self.run_cli(
                ["--config", str(path), "--all", "--sort", "--no-credits"]
            )

        self.assertEqual(0, code)
        self.assertEqual(["a", "b", "c"], self.ids(configfile.load_document(path), "iris"))

    def test_noop_run_leaves_the_file_untouched(self):
        path = self.config_path(
            build_patch({"iris": route(models=[{"id": "a"}, {"id": "b"}])})
        )
        before = path.read_bytes()
        with fake_network(FakeFetch({"x.test": ["a", "b"]})):
            code, out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(0, code)
        self.assertIn("[unchanged] iris", out)
        self.assertEqual(before, path.read_bytes())

    def test_unsafe_models_shapes_are_never_written(self):
        for shape, block in UnsafeModelsTest.SHAPES.items():
            with self.subTest(shape=shape):
                path = self.config_path(patch_with_models(block))
                before = path.read_bytes()
                fetch = FakeFetch({"x.test": ["alpha", "beta"]})
                with fake_network(fetch):
                    code, out, _err = self.run_cli(
                        ["--config", str(path), "--all", "--no-credits"]
                    )
                self.assertEqual(1, code, shape)
                self.assertIn("[error] iris", out, shape)
                self.assertEqual(before, path.read_bytes(), f"{shape}: file was rewritten")
                self.assertEqual([], fetch.calls, f"{shape}: reached the endpoint")


class CliReportTest(CliCase):
    def test_report_lists_added_and_removed(self):
        path = self.config_path(
            build_patch({"iris": route(models=[{"id": "old-chat"}, {"id": "stale"}])})
        )
        with fake_network(FakeFetch({"x.test": ["old-chat", "new-chat"]})):
            code, out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(0, code)
        self.assertRegex(out, r"added \(\d+\)")
        self.assertRegex(out, r"removed \(\d+\)")
        self.assertIn("new-chat", out)
        self.assertIn("stale", out)
        self.assertIn("[changes] iris", out)

    def test_report_shows_kept_count_for_unchanged_routes(self):
        path = self.config_path(
            build_patch({"iris": route(models=[{"id": "a"}, {"id": "b"}])})
        )
        with fake_network(FakeFetch({"x.test": ["a", "b"]})):
            code, out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(0, code)
        self.assertIn("[unchanged] iris", out)
        self.assertRegex(out, r"kept 2 models")
        self.assertRegex(out, r"Summary: 1 routes")

    def test_report_lists_filtered_non_text_models(self):
        path = self.config_path()
        with fake_network(FakeFetch({"x.test": ["old-chat", "sora2"]})):
            code, out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(0, code)
        self.assertIn("filtered non-text", out)
        self.assertIn("sora2", out)

    def test_report_shows_no_text_signal(self):
        path = self.config_path()
        with fake_network(FakeFetch({"x.test": ["tts1"]})):
            code, out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(0, code)
        self.assertIn(
            "no text signal (deletion skipped)",
            [line.strip() for line in out.splitlines()],
        )

    def test_report_lists_discarded_invalid_entries(self):
        path = self.config_path(
            build_patch({"iris": route(models=[{"id": "old-chat"}, {"name": "broken"}])})
        )
        with fake_network(FakeFetch({"x.test": ["old-chat"]})):
            code, out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(0, code)
        self.assertIn("discarded invalid entries", out)

    def test_report_warns_when_a_route_has_no_endpoint(self):
        path = self.config_path(
            build_patch({"iris": route(models=[{"id": "keep"}], base_url=None)})
        )
        with fake_network(FakeFetch({})):
            code, out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(1, code)
        self.assertIn("warning: no endpoint url", out)

    def test_report_mentions_deletion_skipped(self):
        path = self.config_path(
            build_patch({"iris": route(models=[{"id": "old-chat"}, {"id": "stale"}])})
        )
        with fake_network(FakeFetch({"x.test": ["old-chat"]})):
            code, out, _err = self.run_cli(
                ["--config", str(path), "--all", "--no-delete", "--no-credits"]
            )

        self.assertEqual(0, code)
        self.assertIn("deletion skipped", out)

    def test_report_shows_credits(self):
        path = self.config_path()
        with fake_network(FakeFetch({"x.test": ["old-chat"]}), credits=lambda *a, **k: "CNY 12.34"):
            with patch.object(sync_module, "known_credit_host", return_value="test-host"):
                code, out, _err = self.run_cli(["--config", str(path), "--all"])

        self.assertEqual(0, code)
        self.assertIn("credits: CNY 12.34", out)

    def test_no_credits_flag_skips_the_lookup(self):
        path = self.config_path()
        calls: list = []
        with fake_network(
            FakeFetch({"x.test": ["old-chat"]}), credits=lambda *a, **k: calls.append(a)
        ):
            with patch.object(sync_module, "known_credit_host", return_value="test-host"):
                code, out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(0, code)
        self.assertNotIn("credits:", out)
        self.assertEqual([], calls)

    def test_summary_counts_routes(self):
        path = self.config_path(
            build_patch(
                {
                    "iris": route(models=[{"id": "i"}]),
                    "nvidia": route(models=[{"id": "n"}], base_url="https://n.test/v1"),
                }
            )
        )
        fetch = FakeFetch(
            {"https://x.test/v1/models": ["i", "i-new"], "https://n.test/v1/models": ["n"]}
        )
        with fake_network(fetch):
            code, out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(0, code)
        self.assertRegex(out, r"Summary: 2 routes, 1 changed \(\+1 -0\), 1 unchanged")

    def test_summary_counts_failed_and_changed_separately(self):
        """A failed route that still cleaned invalid entries is ``changed`` and
        ``failed`` at once; ``unchanged`` must not go negative."""
        path = self.config_path(
            build_patch({"iris": route(models=[{"id": "ok"}, {"name": "broken"}])})
        )
        with fake_network(FakeFetch({"x.test": "HTTP 401"})):
            code, out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(1, code)
        summary = next(line for line in out.splitlines() if line.startswith("Summary:"))
        self.assertEqual(
            "Summary: 1 routes, 0 changed (+0 -0), 0 unchanged, 1 failed", summary
        )

    def test_summary_stays_consistent_for_four_routes(self):
        path = self.config_path(
            build_patch(
                {
                    "iris": route(models=[{"id": "old"}, {"id": "stale"}]),
                    "nvidia": route(models=[{"id": "nv"}], base_url="https://n.test/v1"),
                    "broken": route(models=[{"id": "b"}], base_url="https://bad.test/v1"),
                    "no-url": route(models=[{"id": "c"}], base_url=None),
                }
            )
        )
        fetch = FakeFetch(
            {
                "https://x.test/v1/models": ["old", "new-a", "new-b"],
                "https://n.test/v1/models": ["nv"],
                "https://bad.test/v1/models": "HTTP 401 Unauthorized",
            }
        )
        with fake_network(fetch):
            code, out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(1, code)
        summary = next(line for line in out.splitlines() if line.startswith("Summary:"))
        self.assertEqual(
            "Summary: 4 routes, 1 changed (+2 -1), 1 unchanged, 2 failed", summary
        )

    def test_failed_route_still_reports_discarded_entries(self):
        path = self.config_path(
            build_patch({"iris": route(models=[{"id": "ok"}, {"name": "broken"}])})
        )
        with fake_network(FakeFetch({"x.test": "HTTP 401"})):
            code, out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(1, code)
        lines = out.splitlines()
        self.assertIn("[error] iris", lines)
        self.assertIn("  error: HTTP 401", lines)
        self.assertTrue(
            any("discarded invalid entries" in line for line in lines),
            f"discarded entries missing from report: {lines}",
        )

    def test_no_endpoint_route_warns_and_discards_invalid_entries(self):
        path = self.config_path(
            build_patch(
                {"iris": route(models=[{"id": "ok"}, {"name": "broken"}], base_url=None)}
            )
        )
        with fake_network(FakeFetch({})):
            code, out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(1, code)
        self.assertIn("[error] iris", out)
        self.assertIn("warning: no endpoint url", out)
        self.assertNotIn("error: no endpoint url", out)
        self.assertIn("discarded invalid entries", out)
        self.assertEqual(["ok"], self.ids(configfile.load_document(path), "iris"))

    def test_report_ends_with_the_config_path_line(self):
        path = self.config_path()
        with fake_network(FakeFetch({"x.test": ["old-chat"]})):
            code, out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(0, code)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(f"config: {path}", lines[-1])


class CliColoringTest(CliCase):
    def test_tty_output_is_coloured(self):
        path = self.config_path()
        with fake_network(FakeFetch({"x.test": ["old-chat", "new-chat"]})):
            code, out, err = self.run_cli(
                ["--config", str(path), "--all", "--no-credits", "--dry-run"], tty=True
            )

        self.assertEqual(0, code)
        self.assertIn("\x1b[", out)
        self.assertEqual("", err)

    def test_non_tty_output_is_plain_and_mirrored_to_stderr(self):
        path = self.config_path()
        with fake_network(FakeFetch({"x.test": ["old-chat", "new-chat"]})):
            code, out, err = self.run_cli(
                ["--config", str(path), "--all", "--no-credits", "--dry-run"]
            )

        self.assertEqual(0, code)
        self.assertNotIn("\x1b[", out)
        self.assertNotIn("\x1b[", err)
        self.assertIn("Summary:", err)
        self.assertIn("Summary:", out)

    def test_no_color_env_disables_colour(self):
        path = self.config_path()
        with fake_network(FakeFetch({"x.test": ["old-chat", "new-chat"]})):
            code, out, err = self.run_cli(
                ["--config", str(path), "--all", "--no-credits", "--dry-run"],
                tty=True,
                env={"NO_COLOR": "1"},
            )

        self.assertEqual(0, code)
        self.assertNotIn("\x1b[", out)
        self.assertNotIn("\x1b[", err)

    def test_no_color_flag_disables_colour(self):
        path = self.config_path()
        with fake_network(FakeFetch({"x.test": ["old-chat", "new-chat"]})):
            code, out, err = self.run_cli(
                ["--config", str(path), "--all", "--no-credits", "--dry-run", "--no-color"],
                tty=True,
            )

        self.assertEqual(0, code)
        self.assertNotIn("\x1b[", out)
        self.assertNotIn("\x1b[", err)


class ColorEnabledTest(CliCase):
    """``cli.color_enabled`` decision matrix, driven through ``sys.stdout``."""

    def test_tty_without_no_color_enables_colour(self):
        with clean_env(), patch.object(sys, "stdout", CliStream(tty=True)):
            self.assertTrue(cli.color_enabled())

    def test_non_tty_disables_colour(self):
        with clean_env(), patch.object(sys, "stdout", CliStream(tty=False)):
            self.assertFalse(cli.color_enabled())

    def test_no_color_env_disables_colour_even_on_a_tty(self):
        with clean_env(NO_COLOR="1"), patch.object(sys, "stdout", CliStream(tty=True)):
            self.assertFalse(cli.color_enabled())

    def test_empty_no_color_env_still_disables_colour(self):
        # ``NO_COLOR=`` (present but empty) must count as "set".
        with clean_env(NO_COLOR=""), patch.object(sys, "stdout", CliStream(tty=True)):
            self.assertFalse(cli.color_enabled())


if __name__ == "__main__":
    unittest.main()
