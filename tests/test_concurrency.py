"""Concurrency, determinism, isolation, and CLI parity tests for dsh-byok-sync.

All tests are strictly offline: network calls go through in-memory stubs and
temporary config files live inside workspace-local directories via
:mod:`tests._helpers`.
"""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

try:  # `python -m unittest discover -s tests`
    import _helpers  # noqa: F401
    from test_sync import (
        CliCase,
        FakeFetch,
        NoCredentials,
        SyncCase,
        build_patch,
        build_settings,
        fake_network,
        route,
    )
except ImportError:  # `python -m unittest tests.test_concurrency`
    from tests import _helpers  # noqa: F401
    from tests.test_sync import (
        CliCase,
        FakeFetch,
        NoCredentials,
        SyncCase,
        build_patch,
        build_settings,
        fake_network,
        route,
    )

from dsh_byok_sync import cli, configfile, sync as sync_module
from dsh_byok_sync.models import FetchResult, RemoteModel


class _MappingCredentials:
    """In-memory credential store that records every resolve call and thread."""

    def __init__(self, keys: dict[str, str]):
        self._keys = dict(keys)
        self.calls: list[tuple[str | None, str, int]] = []
        self._lock = threading.Lock()

    def resolve(self, api_key_env, route_name):
        with self._lock:
            self.calls.append((api_key_env, route_name, threading.get_ident()))
        if not api_key_env:
            return None
        return self._keys.get(api_key_env)


def _rich_patch_text() -> str:
    """Build a multi-route cordis.patch.yml covering every sync contract."""
    return (
        "- id: llm-pi-ai\n"
        '  name: "@deepseek-ai/dsh-llm-pi-ai"\n'
        "  config:\n"
        "    providers:\n"
        "      deepseek:\n"
        "        api: openai-completions\n"
        "        baseURL: https://api.deepseek.com/v1\n"
        "        apiKeyEnv: DEEPSEEK_KEY\n"
        "        models:\n"
        "          # keep this comment\n"
        "          - id: deepseek-chat\n"
        "            name: DeepSeek Chat\n"
        "          - id: deepseek-stale\n"
        "            name: Old\n"
        "      openrouter:\n"
        "        api: openai-completions\n"
        "        baseURL: https://openrouter.ai/api/v1\n"
        "        apiKeyEnv: OR_KEY\n"
        "        models:\n"
        "          - id: or-keep\n"
        "            name: OR Keep\n"
        "          - name: missing-id-entry\n"
        "      anthropic:\n"
        "        api: anthropic-messages\n"
        "        baseURL: https://anth.test/v1\n"
        "        models:\n"
        "          - id: claude-keep\n"
        "            name: Claude Keep\n"
        "      gemini:\n"
        "        api: google-generative-ai\n"
        "        baseURL: https://gem.test/v1beta\n"
        "        models:\n"
        "          - id: gemini-old\n"
        "            name: Gemini Old\n"
        "      media_only:\n"
        "        api: openai-completions\n"
        "        baseURL: https://media.test/v1\n"
        "        models:\n"
        "          - id: local-chat\n"
        "            name: Local Chat\n"
        "      broken:\n"
        "        api: openai-completions\n"
        "        baseURL: https://broken.test/v1\n"
        "        models:\n"
        "          - id: broken-keep\n"
        "            name: Broken Keep\n"
        "      no_url:\n"
        "        api: openai-completions\n"
        "        models:\n"
        "          - id: orphan-keep\n"
        "            name: Orphan Keep\n"
        "      refused:\n"
        "        api: openai-completions\n"
        "        baseURL: https://refused.test/v1\n"
        "        models: [alpha, beta]\n"
    )


_RICH_URL_ORDER = (
    "https://api.deepseek.com/v1/models",
    "https://openrouter.ai/api/v1/models",
    "https://anth.test/v1/models?limit=1000",
    "https://gem.test/v1beta/models?pageSize=1000",
    "https://media.test/v1/models",
    "https://broken.test/v1/models",
)

_RICH_CREDIT_ORDER = (
    "https://api.deepseek.com/v1",
    "https://openrouter.ai/api/v1",
)

_RICH_MODEL_TABLE = {
    "https://api.deepseek.com/v1/models": [
        "deepseek-chat",
        "deepseek-v4-reasoner",
        "sora2",
        RemoteModel(id="vision-chat", name="图像理解"),
    ],
    "https://openrouter.ai/api/v1/models": [
        "or-keep",
        RemoteModel(id="or-new", name="OR New", contextWindow=128000, maxTokens=16384),
        RemoteModel(id="hidden-audio", name="Whisper Turbo"),
    ],
    "https://anth.test/v1/models?limit=1000": ["claude-keep"],
    "https://gem.test/v1beta/models?pageSize=1000": ["gemini-new"],
    "https://media.test/v1/models": ["tts1", "video-gen"],
    "https://broken.test/v1/models": "HTTP 401; check the API key",
}


def _reversed_delay(key: str, order: tuple[str, ...], scale: float = 0.008) -> float:
    """Earlier tasks sleep longer so concurrent completions invert submission order."""
    if key not in order:
        return 0.0
    return (len(order) - order.index(key)) * scale


def _make_rich_fakes(*, delay_scale: float = 0.008):
    base_fetch = FakeFetch(_RICH_MODEL_TABLE)

    def fetch(url, headers, timeout):
        delay = _reversed_delay(url, _RICH_URL_ORDER, delay_scale)
        if delay:
            time.sleep(delay)
        return base_fetch(url, headers, timeout)

    def credits(base_url, api_key, headers, timeout):
        delay = _reversed_delay(base_url, _RICH_CREDIT_ORDER, delay_scale)
        if delay:
            time.sleep(delay)
        if "deepseek" in base_url:
            return "CNY 42.50"
        return None  # openrouter -> "unavailable"

    return fetch, credits


# --- worker resolution & run_ordered ---------------------------------------


class ResolveWorkerCountTest(unittest.TestCase):
    def test_none_scales_with_tasks_and_caps_at_default(self):
        self.assertEqual(1, sync_module.resolve_worker_count(None, 0))
        self.assertEqual(1, sync_module.resolve_worker_count(None, 1))
        self.assertEqual(3, sync_module.resolve_worker_count(None, 3))
        self.assertEqual(
            sync_module.DEFAULT_MAX_WORKERS,
            sync_module.resolve_worker_count(None, sync_module.DEFAULT_MAX_WORKERS),
        )
        self.assertEqual(
            sync_module.DEFAULT_MAX_WORKERS,
            sync_module.resolve_worker_count(None, sync_module.DEFAULT_MAX_WORKERS + 12),
        )

    def test_explicit_positive_clamps_to_task_count(self):
        self.assertEqual(1, sync_module.resolve_worker_count(4, 0))
        self.assertEqual(1, sync_module.resolve_worker_count(4, 1))
        self.assertEqual(2, sync_module.resolve_worker_count(4, 2))
        self.assertEqual(4, sync_module.resolve_worker_count(4, 4))
        self.assertEqual(4, sync_module.resolve_worker_count(4, 10))
        self.assertEqual(1, sync_module.resolve_worker_count(1, 10))

    def test_invalid_values_raise_value_error(self):
        for bad in (0, -1, -50, False, True, 1.5, "4"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sync_module.resolve_worker_count(bad, 5)


class RunOrderedTest(unittest.TestCase):
    def test_results_follow_submission_order_not_completion_order(self):
        finished: list[int] = []
        lock = threading.Lock()

        def make_task(idx: int):
            def run():
                time.sleep((5 - idx) * 0.02)
                with lock:
                    finished.append(idx)
                return idx

            return run

        results = sync_module.run_ordered([make_task(i) for i in range(5)], max_workers=5)
        self.assertEqual([0, 1, 2, 3, 4], results)
        self.assertNotEqual([0, 1, 2, 3, 4], finished)

    def test_workers_one_runs_inline_without_thread_pool(self):
        main_tid = threading.get_ident()
        seen: list[tuple[int, int]] = []

        def make_task(idx: int):
            def run():
                seen.append((idx, threading.get_ident()))
                return idx * 10

            return run

        with patch.object(
            sync_module,
            "ThreadPoolExecutor",
            side_effect=AssertionError("ThreadPoolExecutor must not be created for workers=1"),
        ):
            results = sync_module.run_ordered([make_task(i) for i in range(4)], max_workers=1)

        self.assertEqual([0, 10, 20, 30], results)
        self.assertEqual([(i, main_tid) for i in range(4)], seen)

    def test_single_task_with_none_runs_inline_without_thread_pool(self):
        main_tid = threading.get_ident()
        seen_tid: list[int] = []

        def only():
            seen_tid.append(threading.get_ident())
            return "ok"

        with patch.object(
            sync_module,
            "ThreadPoolExecutor",
            side_effect=AssertionError("ThreadPoolExecutor must not be created for 1 task"),
        ):
            self.assertEqual(["ok"], sync_module.run_ordered([only], max_workers=None))
        self.assertEqual([main_tid], seen_tid)

    def test_empty_tasks_validates_max_workers(self):
        self.assertEqual([], sync_module.run_ordered([], max_workers=None))
        self.assertEqual([], sync_module.run_ordered([], max_workers=1))
        with self.assertRaises(ValueError):
            sync_module.run_ordered([], max_workers=0)

    def test_concurrency_peak_respects_max_workers(self):
        active = 0
        peak = 0
        lock = threading.Lock()
        names: list[str] = []

        def task():
            nonlocal active, peak
            with lock:
                active += 1
                if active > peak:
                    peak = active
                names.append(threading.current_thread().name)
            try:
                time.sleep(0.03)
            finally:
                with lock:
                    active -= 1
            return 1

        results = sync_module.run_ordered([task for _ in range(9)], max_workers=3)
        self.assertEqual([1] * 9, results)
        self.assertEqual(3, peak)
        self.assertTrue(all(name.startswith("dsh-byok-sync") for name in names), names)

    def test_unhandled_exception_propagates_cleanly(self):
        def ok():
            return 1

        def boom():
            raise RuntimeError("raw task failure")

        with self.assertRaisesRegex(RuntimeError, "raw task failure"):
            sync_module.run_ordered([ok, boom, ok], max_workers=3)


# --- determinism -----------------------------------------------------------


class DeterminismTest(SyncCase):
    def _run_rich(self, max_workers, *, sort=False, no_delete=False, delay_scale=0.008):
        document = self.document(_rich_patch_text())
        before_text = document.text
        fetch, credits = _make_rich_fakes(delay_scale=delay_scale)
        outcome = sync_module.sync_config(
            document,
            fetch=fetch,
            credits_fetch=credits,
            credentials=_MappingCredentials({"DEEPSEEK_KEY": "sk-ds", "OR_KEY": "sk-or"}),
            sort=sort,
            no_delete=no_delete,
            max_workers=max_workers,
        )
        self.assertEqual(before_text, document.text)
        rendered = cli.render(outcome, "<cfg>", color=False)
        return outcome, rendered

    def test_fixture_exercises_every_contract(self):
        outcome, rendered = self._run_rich(max_workers=1, delay_scale=0.0)
        by_route = {report.route: report for report in outcome.reports}

        # DeepSeek: addition (with preset + 图像理解 exemption), deletion, filter, credits
        ds = by_route["deepseek"]
        self.assertEqual(("deepseek-v4-reasoner", "vision-chat"), ds.added)
        self.assertEqual(("deepseek-stale",), ds.removed)
        self.assertEqual(("sora2",), ds.filtered)
        self.assertEqual("CNY 42.50", ds.credits)
        self.assertTrue(ds.changed)

        # OpenRouter: addition, filtered by display name, discarded invalid entry, unavailable credits
        or_rep = by_route["openrouter"]
        self.assertEqual(("or-new",), or_rep.added)
        self.assertEqual(("hidden-audio",), or_rep.filtered)
        self.assertEqual(1, len(or_rep.discarded))
        self.assertEqual("unavailable", or_rep.credits)

        # Unchanged Anthropic
        self.assertFalse(by_route["anthropic"].changed)
        self.assertEqual(1, by_route["anthropic"].kept)

        # Gemini replacement
        self.assertEqual(("gemini-new",), by_route["gemini"].added)
        self.assertEqual(("gemini-old",), by_route["gemini"].removed)

        # Media-only -> no text signal blocks deletion
        self.assertTrue(by_route["media_only"].no_text_signal)
        self.assertEqual((), by_route["media_only"].removed)

        # Error, missing baseURL, refused flow style
        self.assertIn("401", by_route["broken"].error)
        self.assertEqual("no endpoint url", by_route["no_url"].error)
        self.assertIn("流风格", by_route["refused"].error)

        # Comment preserved & DeepSeek preset applied in rewritten YAML
        self.assertIn("# keep this comment", outcome.new_text)
        self.assertIn("reasoningEfforts:", outcome.new_text)
        self.assertTrue(rendered[-1].startswith("config:"))

    def test_sync_config_is_deterministic_across_worker_counts(self):
        baseline_outcome, baseline_rendered = self._run_rich(max_workers=1)
        for workers in (1, 2, 4, 8, None):
            for rep in range(3):
                with self.subTest(workers=workers, rep=rep):
                    outcome, rendered = self._run_rich(max_workers=workers)
                    self.assertEqual(baseline_outcome.reports, outcome.reports)
                    self.assertEqual(baseline_outcome.new_text, outcome.new_text)
                    self.assertEqual(baseline_rendered, rendered)

    def test_sort_and_no_delete_are_deterministic_across_workers(self):
        for flags in ({"sort": True}, {"no_delete": True}, {"sort": True, "no_delete": True}):
            baseline_outcome, baseline_rendered = self._run_rich(max_workers=1, **flags)
            for workers in (2, 4, None):
                with self.subTest(flags=flags, workers=workers):
                    outcome, rendered = self._run_rich(max_workers=workers, **flags)
                    self.assertEqual(baseline_outcome.reports, outcome.reports)
                    self.assertEqual(baseline_outcome.new_text, outcome.new_text)
                    self.assertEqual(baseline_rendered, rendered)

    def test_settings_yaml_is_deterministic_across_workers(self):
        text = build_settings(
            {
                "r1": route(models=[{"id": "b"}, {"id": "a"}], base_url="https://api.deepseek.com/v1"),
                "r2": route(models=[{"id": "x"}, {"id": "stale"}], base_url="https://r2.test/v1"),
                "r3": route(models=[{"id": "k"}], base_url="https://r3.test/v1"),
            }
        )
        order = (
            "https://api.deepseek.com/v1/models",
            "https://r2.test/v1/models",
            "https://r3.test/v1/models",
        )
        base = FakeFetch(
            {
                order[0]: ["b", "a", "c"],
                order[1]: ["x", "y"],
                order[2]: ["k"],
            }
        )

        def fetch(url, headers, timeout):
            time.sleep(_reversed_delay(url, order, 0.01))
            return base(url, headers, timeout)

        def credits(base_url, api_key, headers, timeout):
            time.sleep(0.01)
            return "CNY 8.00"

        def run(workers):
            doc = self.document(text, name="settings.yaml")
            return sync_module.sync_config(
                doc,
                fetch=fetch,
                credits_fetch=credits,
                credentials=NoCredentials(),
                max_workers=workers,
            )

        serial = run(1)
        for workers in (2, 4, None):
            with self.subTest(workers=workers):
                concurrent = run(workers)
                self.assertEqual(serial.reports, concurrent.reports)
                self.assertEqual(serial.new_text, concurrent.new_text)


# --- speedup & single-stage merged concurrency -----------------------------


class SpeedupAndMergedPoolTest(SyncCase):
    def test_multiple_routes_run_faster_concurrently(self):
        routes = {
            f"r{i}": route(models=[{"id": f"m{i}"}], base_url=f"https://r{i}.test/v1")
            for i in range(6)
        }
        text = build_patch(routes)

        def slow_fetch(url, headers, timeout):
            time.sleep(0.15)
            idx = int(url.split("//r")[1].split(".")[0])
            return FetchResult(url=url, models=(RemoteModel(id=f"m{idx}"),))

        def measure(workers):
            doc = self.document(text)
            start = time.monotonic()
            outcome = sync_module.sync_config(
                doc,
                fetch=slow_fetch,
                use_credits=False,
                credentials=NoCredentials(),
                max_workers=workers,
            )
            return time.monotonic() - start, outcome

        t_concurrent, out_c = measure(6)
        t_auto, out_a = measure(None)
        t_serial, out_s = measure(1)

        self.assertEqual(out_s.reports, out_c.reports)
        self.assertEqual(out_s.reports, out_a.reports)
        self.assertLess(t_concurrent, 0.45)
        self.assertLess(t_auto, 0.45)
        self.assertGreaterEqual(t_serial, 0.85)

    def test_models_and_credits_overlap_in_single_shared_pool(self):
        """All model and credit requests enter the same pool and run concurrently."""
        text = build_patch(
            {
                "ds": route(models=[{"id": "ds-1"}], base_url="https://api.deepseek.com/v1"),
                "or": route(models=[{"id": "or-1"}], base_url="https://openrouter.ai/api/v1"),
                "ms": route(models=[{"id": "ms-1"}], base_url="https://api.moonshot.cn/v1"),
            }
        )
        doc = self.document(text)
        # 3 model requests + 3 credit requests = 6 tasks; all 6 must arrive at the
        # barrier simultaneously, which is impossible under two-phase or serial execution.
        barrier = threading.Barrier(6, timeout=2.0)

        def fetch(url, headers, timeout):
            barrier.wait()
            if "deepseek" in url:
                return FetchResult(url=url, models=(RemoteModel(id="ds-1"),))
            if "openrouter" in url:
                return FetchResult(url=url, models=(RemoteModel(id="or-1"),))
            return FetchResult(url=url, models=(RemoteModel(id="ms-1"),))

        def credits(base_url, api_key, headers, timeout):
            barrier.wait()
            if "deepseek" in base_url:
                return "CNY 10.00"
            if "openrouter" in base_url:
                return "USD 5.00"
            return "CNY 20.00"

        outcome = sync_module.sync_config(
            doc,
            fetch=fetch,
            credits_fetch=credits,
            credentials=NoCredentials(),
            max_workers=None,
        )
        by_route = {r.route: r for r in outcome.reports}
        self.assertEqual("CNY 10.00", by_route["ds"].credits)
        self.assertEqual("USD 5.00", by_route["or"].credits)
        self.assertEqual("CNY 20.00", by_route["ms"].credits)

    def test_single_stage_wall_time_beats_two_phase_latency(self):
        """3 models (0.2s) + 3 credits (0.2s) finish in ~0.2s, faster than 0.4s two-phase."""
        text = build_patch(
            {
                "ds": route(models=[{"id": "ds-1"}], base_url="https://api.deepseek.com/v1"),
                "or": route(models=[{"id": "or-1"}], base_url="https://openrouter.ai/api/v1"),
                "ms": route(models=[{"id": "ms-1"}], base_url="https://api.moonshot.cn/v1"),
            }
        )
        doc = self.document(text)

        def fetch(url, headers, timeout):
            time.sleep(0.22)
            return FetchResult(url=url, models=(RemoteModel(id="m"),))

        def credits(base_url, api_key, headers, timeout):
            time.sleep(0.22)
            return "CNY 1.00"

        start = time.monotonic()
        sync_module.sync_config(
            doc,
            fetch=fetch,
            credits_fetch=credits,
            credentials=NoCredentials(),
            max_workers=None,
        )
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.40)

    def test_merged_pool_respects_worker_cap(self):
        text = build_patch(
            {
                "ds": route(models=[{"id": "a"}], base_url="https://api.deepseek.com/v1"),
                "or": route(models=[{"id": "b"}], base_url="https://openrouter.ai/api/v1"),
                "ms": route(models=[{"id": "c"}], base_url="https://api.moonshot.cn/v1"),
                "st": route(models=[{"id": "d"}], base_url="https://api.stepfun.com/v1"),
            }
        )
        doc = self.document(text)
        active = 0
        peak = 0
        lock = threading.Lock()

        def track(work):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.04)
                return work()
            finally:
                with lock:
                    active -= 1

        outcome = sync_module.sync_config(
            doc,
            fetch=lambda url, h, t: track(
                lambda: FetchResult(url=url, models=(RemoteModel(id="x"),))
            ),
            credits_fetch=lambda b, k, h, t: track(lambda: "CNY 1.00"),
            credentials=NoCredentials(),
            max_workers=3,
        )
        self.assertEqual(4, len(outcome.reports))
        self.assertEqual(3, peak)


# --- failure isolation -----------------------------------------------------


class FailureIsolationTest(SyncCase):
    def test_fetch_exception_isolated_blocks_deletion_and_keeps_other_routes_working(self):
        text = build_patch(
            {
                "exploding": route(
                    models=[{"id": "keep-safe"}, {"name": "invalid-no-id"}],
                    base_url="https://boom.test/v1",
                ),
                "healthy": route(
                    models=[{"id": "h-keep"}, {"id": "h-stale"}],
                    base_url="https://ok.test/v1",
                ),
            }
        )

        def fetch(url, headers, timeout):
            if "boom" in url:
                raise RuntimeError("socket kaboom")
            return FetchResult(url=url, models=(RemoteModel(id="h-keep"), RemoteModel(id="h-new")))

        for workers in (1, 4):
            with self.subTest(workers=workers):
                doc = self.document(text)
                outcome = sync_module.sync_config(
                    doc,
                    fetch=fetch,
                    use_credits=False,
                    credentials=NoCredentials(),
                    max_workers=workers,
                )
                boom = self.report(outcome, "exploding")
                ok = self.report(outcome, "healthy")

                self.assertEqual("unexpected error: socket kaboom", boom.error)
                self.assertEqual((), boom.removed)
                self.assertEqual(1, boom.kept)
                self.assertEqual(1, len(boom.discarded))
                self.assertTrue(boom.changed)  # invalid entry cleaned

                self.assertIsNone(ok.error)
                self.assertEqual(("h-new",), ok.added)
                self.assertEqual(("h-stale",), ok.removed)

                reparsed = self.rewritten(doc, outcome)
                self.assertEqual(["keep-safe"], self.ids(reparsed, "exploding"))
                self.assertEqual(["h-keep", "h-new"], self.ids(reparsed, "healthy"))

    def test_fetch_exception_without_message_uses_class_name(self):
        doc = self.document(build_patch({"iris": route(models=[{"id": "a"}])}))

        def fetch(url, headers, timeout):
            raise RuntimeError()

        outcome = sync_module.sync_config(
            doc, fetch=fetch, use_credits=False, credentials=NoCredentials(), max_workers=2
        )
        self.assertEqual("unexpected error: RuntimeError", self.report(outcome, "iris").error)

    def test_fetch_exception_redacts_api_key(self):
        secret = "sk-top-secret-token-987654321"
        doc = self.document(
            build_patch({"iris": route(models=[{"id": "a"}], apiKeyEnv="IRIS_SECRET")})
        )

        def fetch(url, headers, timeout):
            raise RuntimeError(f"auth failed with {headers['Authorization']}\nsecond line")

        outcome = sync_module.sync_config(
            doc,
            fetch=fetch,
            use_credits=False,
            credentials=_MappingCredentials({"IRIS_SECRET": secret}),
            max_workers=2,
        )
        error = self.report(outcome, "iris").error
        self.assertIsNotNone(error)
        self.assertNotIn(secret, error)
        self.assertNotIn("\n", error)
        self.assertIn("<redacted>", error)

    def test_credits_exception_degrades_to_unavailable(self):
        text = build_patch(
            {
                "ds": route(models=[{"id": "ds-1"}], base_url="https://api.deepseek.com/v1"),
                "or": route(models=[{"id": "or-1"}], base_url="https://openrouter.ai/api/v1"),
            }
        )

        def credits(base_url, api_key, headers, timeout):
            if "deepseek" in base_url:
                raise RuntimeError("balance boom")
            return "USD 9.00"

        for workers in (1, 4):
            with self.subTest(workers=workers):
                doc = self.document(text)
                outcome = sync_module.sync_config(
                    doc,
                    fetch=FakeFetch(
                        {
                            "https://api.deepseek.com/v1/models": ["ds-1"],
                            "https://openrouter.ai/api/v1/models": ["or-1"],
                        }
                    ),
                    credits_fetch=credits,
                    credentials=NoCredentials(),
                    max_workers=workers,
                )
                self.assertEqual((), outcome.failed)
                self.assertEqual("unavailable", self.report(outcome, "ds").credits)
                self.assertEqual("USD 9.00", self.report(outcome, "or").credits)

    def test_invalid_max_workers_raises_even_without_network_routes(self):
        doc = self.document(build_patch({"no_url": route(models=[{"id": "a"}], base_url=None)}))
        with self.assertRaises(ValueError):
            sync_module.sync_config(
                doc,
                fetch=FakeFetch({}),
                credentials=NoCredentials(),
                max_workers=0,
            )


# --- credential resolution & thread safety ---------------------------------


class CredentialAndHeaderSafetyTest(SyncCase):
    def test_credentials_resolved_on_main_thread_before_any_fetch(self):
        text = build_patch(
            {
                "ds": route(
                    models=[{"id": "a"}],
                    base_url="https://api.deepseek.com/v1",
                    apiKeyEnv="DS_KEY",
                ),
                "or": route(
                    models=[{"id": "b"}],
                    base_url="https://openrouter.ai/api/v1",
                    apiKeyEnv="OR_KEY",
                ),
                "r3": route(models=[{"id": "c"}], base_url="https://r3.test/v1", apiKeyEnv="R3_KEY"),
                "r4": route(models=[{"id": "d"}], base_url="https://r4.test/v1", apiKeyEnv="R4_KEY"),
            }
        )
        doc = self.document(text)
        main_tid = threading.get_ident()
        events: list[tuple[str, str, int, str]] = []
        lock = threading.Lock()

        class TrackingCredentials:
            def resolve(self, api_key_env, route_name):
                with lock:
                    events.append(
                        ("resolve", route_name, threading.get_ident(), threading.current_thread().name)
                    )
                return f"key-for-{route_name}"

        def fetch(url, headers, timeout):
            with lock:
                events.append(("fetch", url, threading.get_ident(), threading.current_thread().name))
            time.sleep(0.02)
            return FetchResult(url=url, models=(RemoteModel(id="m"),))

        def credits(base_url, api_key, headers, timeout):
            with lock:
                events.append(
                    ("credits", base_url, threading.get_ident(), threading.current_thread().name)
                )
            time.sleep(0.02)
            return "CNY 1.00"

        sync_module.sync_config(
            doc,
            fetch=fetch,
            credits_fetch=credits,
            credentials=TrackingCredentials(),
            max_workers=None,
        )

        resolve_events = [e for e in events if e[0] == "resolve"]
        network_events = [e for e in events if e[0] in ("fetch", "credits")]

        # One resolve per route, in exact selection order, all on main thread.
        self.assertEqual(["ds", "or", "r3", "r4"], [e[1] for e in resolve_events])
        self.assertTrue(all(e[2] == main_tid for e in resolve_events), resolve_events)

        # Every resolve happened strictly before the first network request.
        last_resolve = max(i for i, e in enumerate(events) if e[0] == "resolve")
        first_network = min(i for i, e in enumerate(events) if e[0] in ("fetch", "credits"))
        self.assertLess(last_resolve, first_network)

        # All network requests executed on dsh-byok-sync worker threads.
        self.assertEqual(6, len(network_events))
        self.assertTrue(all(e[2] != main_tid for e in network_events), network_events)
        self.assertTrue(all(e[3].startswith("dsh-byok-sync") for e in network_events), network_events)

    def test_non_network_routes_do_not_resolve_credentials(self):
        text = (
            "- id: llm-pi-ai\n"
            "  config:\n"
            "    providers:\n"
            "      refused:\n"
            "        api: openai-completions\n"
            "        baseURL: https://refused.test/v1\n"
            "        apiKeyEnv: REFUSED_KEY\n"
            "        models: [a, b]\n"
            "      no_url:\n"
            "        api: openai-completions\n"
            "        apiKeyEnv: NO_URL_KEY\n"
            "        models:\n"
            "          - id: keep\n"
            "      ok:\n"
            "        api: openai-completions\n"
            "        baseURL: https://ok.test/v1\n"
            "        apiKeyEnv: OK_KEY\n"
            "        models:\n"
            "          - id: keep\n"
        )
        doc = self.document(text)
        creds = _MappingCredentials({"OK_KEY": "sk-ok"})
        sync_module.sync_config(
            doc,
            fetch=FakeFetch({"https://ok.test/v1/models": ["keep"]}),
            use_credits=False,
            credentials=creds,
            max_workers=None,
        )
        self.assertEqual([("OK_KEY", "ok")], [(env, r) for env, r, _tid in creds.calls])

    def test_header_dict_not_shared_between_model_and_credit_tasks(self):
        doc = self.document(
            build_patch(
                {
                    "ds": route(
                        models=[{"id": "a"}],
                        base_url="https://api.deepseek.com/v1",
                        apiKeyEnv="DS_KEY",
                    )
                }
            )
        )
        seen_credit_headers: dict[str, str] = {}

        def mutating_fetch(url, headers, timeout):
            headers["Authorization"] = "Bearer CORRUPTED"
            headers["X-Mutated"] = "yes"
            return FetchResult(url=url, models=(RemoteModel(id="a"),))

        def inspecting_credits(base_url, api_key, headers, timeout):
            time.sleep(0.02)
            seen_credit_headers.update(headers)
            return "CNY 1.00"

        sync_module.sync_config(
            doc,
            fetch=mutating_fetch,
            credits_fetch=inspecting_credits,
            credentials=_MappingCredentials({"DS_KEY": "sk-real"}),
            max_workers=2,
        )
        self.assertEqual("Bearer sk-real", seen_credit_headers.get("Authorization"))
        self.assertNotIn("X-Mutated", seen_credit_headers)


# --- CLI concurrency & parity ----------------------------------------------


class CliConcurrencyTest(CliCase):
    def test_cli_output_and_written_file_identical_across_workers(self):
        def execute(extra_args):
            path = self.config_path(_rich_patch_text())
            fetch, credits = _make_rich_fakes(delay_scale=0.008)
            writes: list[str] = []
            real_write = cli.write_text_atomic

            def counting_write(target, text, *, bom=False):
                writes.append(str(target))
                return real_write(target, text, bom=bom)

            with fake_network(fetch, credits=credits), patch.object(
                cli, "write_text_atomic", side_effect=counting_write
            ):
                code, out, err = self.run_cli(["--config", str(path), "--all", *extra_args])

            return (
                code,
                out.replace(str(path), "<cfg>"),
                err.replace(str(path), "<cfg>"),
                path.read_bytes(),
                len(writes),
            )

        baseline = execute(["--workers", "1"])
        self.assertEqual(1, baseline[0])  # broken + no_url + refused -> failed routes
        self.assertEqual(1, baseline[4])  # exactly one atomic write

        for extra in ([], ["--workers", "2"], ["--workers", "4"], ["--workers", "8"]):
            with self.subTest(extra=extra):
                self.assertEqual(baseline, execute(extra))

    def test_cli_writes_once_strictly_after_all_network_calls_complete(self):
        text = build_patch(
            {
                "ds": route(models=[{"id": "a"}], base_url="https://api.deepseek.com/v1"),
                "or": route(models=[{"id": "b"}], base_url="https://openrouter.ai/api/v1"),
                "r3": route(models=[{"id": "c"}], base_url="https://r3.test/v1"),
            }
        )
        path = self.config_path(text)
        timeline: list[str] = []
        lock = threading.Lock()
        real_write = cli.write_text_atomic

        def fetch(url, headers, timeout):
            time.sleep(0.02)
            with lock:
                timeline.append(f"fetch:{url}")
            return FetchResult(url=url, models=(RemoteModel(id="added-model"),))

        def credits(base_url, api_key, headers, timeout):
            time.sleep(0.03)
            with lock:
                timeline.append(f"credits:{base_url}")
            return "CNY 5.00"

        def record_write(target, content, *, bom=False):
            with lock:
                timeline.append("write")
            return real_write(target, content, bom=bom)

        with fake_network(fetch, credits=credits), patch.object(
            cli, "write_text_atomic", side_effect=record_write
        ):
            code, _out, _err = self.run_cli(["--config", str(path), "--all"])

        self.assertEqual(0, code)
        self.assertEqual(1, timeline.count("write"))
        self.assertEqual("write", timeline[-1])
        self.assertEqual(3, sum(1 for item in timeline if item.startswith("fetch:")))
        self.assertEqual(2, sum(1 for item in timeline if item.startswith("credits:")))

    def test_cli_failure_aggregation_with_exception_and_http_error(self):
        text = build_patch(
            {
                "healthy": route(models=[{"id": "keep"}, {"id": "stale"}], base_url="https://ok.test/v1"),
                "unauthorized": route(models=[{"id": "u1"}], base_url="https://unauth.test/v1"),
                "crashing": route(models=[{"id": "c1"}], base_url="https://crash.test/v1"),
            }
        )
        path = self.config_path(text)

        def fetch(url, headers, timeout):
            if "ok.test" in url:
                return FetchResult(url=url, models=(RemoteModel(id="keep"), RemoteModel(id="fresh")))
            if "unauth.test" in url:
                return FetchResult(url=url, models=(), error="HTTP 401; check the API key")
            raise RuntimeError("boom")

        with fake_network(fetch):
            code, out, err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(1, code)
        self.assertNotIn("\x1b[", err)
        positions = [
            out.index("[changes] healthy"),
            out.index("[error] unauthorized"),
            out.index("[error] crashing"),
        ]
        self.assertEqual(sorted(positions), positions)
        self.assertIn("unexpected error: boom", out)
        self.assertIn("Summary: 3 routes, 1 changed (+1 -1), 0 unchanged, 2 failed", out)
        self.assertIn("Summary: 3 routes, 1 changed (+1 -1), 0 unchanged, 2 failed", err)

    def test_cli_dry_run_and_noop_never_write(self):
        path = self.config_path(
            build_patch(
                {
                    "r1": route(models=[{"id": "a"}], base_url="https://r1.test/v1"),
                    "r2": route(models=[{"id": "b"}], base_url="https://r2.test/v1"),
                }
            )
        )
        fetch_changed = FakeFetch(
            {"https://r1.test/v1/models": ["a", "new"], "https://r2.test/v1/models": ["b"]}
        )
        fetch_noop = FakeFetch(
            {"https://r1.test/v1/models": ["a"], "https://r2.test/v1/models": ["b"]}
        )

        with fake_network(fetch_changed), patch.object(cli, "write_text_atomic") as writer:
            code, _out, _err = self.run_cli(
                ["--config", str(path), "--all", "--dry-run", "--no-credits"]
            )
        self.assertEqual(0, code)
        writer.assert_not_called()

        with fake_network(fetch_noop), patch.object(cli, "write_text_atomic") as writer:
            code, _out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])
        self.assertEqual(0, code)
        writer.assert_not_called()

    def test_cli_no_credits_skips_all_credit_calls(self):
        path = self.config_path(
            build_patch(
                {
                    "ds": route(models=[{"id": "a"}], base_url="https://api.deepseek.com/v1"),
                    "or": route(models=[{"id": "b"}], base_url="https://openrouter.ai/api/v1"),
                }
            )
        )
        credit_calls: list[str] = []
        with fake_network(
            FakeFetch(
                {
                    "https://api.deepseek.com/v1/models": ["a"],
                    "https://openrouter.ai/api/v1/models": ["b"],
                }
            ),
            credits=lambda base_url, *a, **k: credit_calls.append(base_url) or "CNY 1.00",
        ):
            code, out, _err = self.run_cli(["--config", str(path), "--all", "--no-credits"])

        self.assertEqual(0, code)
        self.assertEqual([], credit_calls)
        self.assertNotIn("credits:", out)

    def test_cli_workers_propagated_to_sync_options(self):
        path = self.config_path()
        captured: list[int | None] = []
        real_sync = cli.sync_config

        def spy_sync(document, routes=None, *, options=None, **kwargs):
            captured.append(options.max_workers if options is not None else None)
            return real_sync(document, routes, options=options, **kwargs)

        with fake_network(FakeFetch({"x.test": ["old-chat"]})), patch.object(
            cli, "sync_config", side_effect=spy_sync
        ):
            self.assertEqual(0, self.run_cli(["--config", str(path), "--all", "--no-credits"])[0])
            self.assertEqual(
                0,
                self.run_cli(["--config", str(path), "--all", "--no-credits", "--workers", "1"])[0],
            )
            self.assertEqual(
                0,
                self.run_cli(["--config", str(path), "--all", "--no-credits", "--workers", "5"])[0],
            )

        self.assertEqual([None, 1, 5], captured)

    def test_cli_invalid_workers_exits_two(self):
        path = self.config_path()
        for bad in ("0", "-2", "abc", "1.5"):
            with self.subTest(bad=bad):
                code, _out, err = self.run_cli(["--config", str(path), "--all", "--workers", bad])
                self.assertEqual(2, code)
                self.assertIn("--workers", err)

    def test_cli_help_mentions_workers_and_single_request_timeout(self):
        help_text = cli.build_parser().format_help()
        self.assertIn("--workers", help_text)
        self.assertIn("单个请求", help_text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
