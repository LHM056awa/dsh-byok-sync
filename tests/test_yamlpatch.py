"""yamlpatch 模块测试。

覆盖 SPEC §8 中 test_yamlpatch.py 要求的所有项：patch/settings 两种形态、
注释与空行保留、CRLF、无末行换行、缺 models 键、流风格、缩进步长 2/4、
同 id 重复、[free]x 引号、no-op 原文不变、未知路由报错、Tab/锚点等不安全输入报错，
以及“真实样例片段”的逐字节回归。
"""

from __future__ import annotations

import unittest

try:  # `python -m unittest tests.test_yamlpatch` 时 tests/ 不一定在 sys.path 上
    import _helpers  # noqa: F401  （副作用：把 src/ 放进 sys.path）
except ImportError:  # pragma: no cover - 兜底，两种运行方式都能工作
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import _helpers  # noqa: F401

from dsh_byok_sync import yamlpatch as yp

# --------------------------------------------------------------------------- #
# 测试夹具
# --------------------------------------------------------------------------- #

#: 与真实的 cordis.patch.yml 结构一致（含 2 空格步长、多行 reasoningEfforts、
#: 条目之间的注释、加引号的 id、重复 id），外面还有前后两个无关条目。
PATCH_TEXT = """# Your patch layer for this dsh profile, applied after every bundle layer:
# a top-level YAML array of loader patch entries.
- id: ui-conversation
  name: "@deepseek-ai/dsh-client-ui-conversation"
  config:
    busyEnter: steer
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      agnesai:
        displayName: AgnesAI
        apiKeyEnv: AGNESAI_API_KEY
        api: openai-completions
        baseURL: https://api.vendor.example.com/v1
        models:
          - id: agnes-3.0-flash
            name: agnes-3.0-flash
            contextWindow: 512000
            input:
              - text
              - image
            reasoningEfforts:
              off: null
              low: low
              medium: medium
              high: high
              xhigh: xhigh
              max: max
              minimal: minimal
            maxTokens: 65536
          # 手工保留的免费通道
          - id: agnes-2.0-flash
            name: agnes-2.0-flash
          - id: "[free]agnes-quoted"
            name: "[free]agnes-quoted"
          - id: agnes-3.0-flash
            name: agnes-3.0-flash (重复 id 副本)
      iris:
        displayName: Iris
        apiKeyEnv: IRIS_API_KEY
        api: openai-completions
        baseURL: https://gateway.example.com/v1
        headers:
          X-Title: dsh-byok-sync
          HTTP-Referer: "https://example.com/referer"
        models:
          - id: deepseek-v4-flash
            name: deepseek-v4-flash
          - id: glm-5.3-flash
            name: glm-5.3-flash
- id: ui-theme
  name: "@deepseek-ai/dsh-client-ui-theme"
  config:
    preference: system
"""

#: 形态 B：根是映射（settings.yaml）。
SETTINGS_TEXT = """ui-conversation:
  busyEnter: steer
llm-pi-ai:
  providers:
    iris:
      displayName: Iris
      apiKeyEnv: IRIS_API_KEY
      api: openai-completions
      baseURL: https://gateway.example.com/v1
      models:
        - id: "[free]claude-opus-5"
          name: "[free]claude-opus-5"
        - id: deepseek-v4-flash
          name: deepseek-v4-flash
        - id: deepseek-v4-pro
          name: deepseek-v4-pro
ui-theme:
  preference: system
"""

#: 流风格的 models（空列表 / 映射列表 / 纯标量列表）。
FLOW_TEXT = """- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      iris:
        apiKeyEnv: IRIS_API_KEY
        models: []
      nvidia:
        apiKeyEnv: NVIDIA_API_KEY
        models: [{id: alpha}, {id: beta}]
      gemini:
        apiKeyEnv: GEMINI_API_KEY
        models: [x, y]  # 纯标量列表
        displayName: Gemini
      empty:
        apiKeyEnv: EMPTY_API_KEY
        models:
"""

#: 缩进步长 4（dash 相对 models 键缩进 4 个空格）。
STEP4_TEXT = """- id: llm-pi-ai
  config:
    providers:
      iris:
        apiKeyEnv: IRIS_API_KEY
        models:
            - id: alpha
              name: alpha
            - id: beta
              name: beta
"""

#: 没有 models 键的路由。
NO_MODELS_TEXT = """- id: llm-pi-ai
  config:
    providers:
      iris:
        displayName: Iris
        apiKeyEnv: IRIS_API_KEY
        api: openai-completions
        baseURL: https://gateway.example.com/v1
      nvidia:
        apiKeyEnv: NVIDIA_API_KEY
        models:
          - id: alpha
"""

ENTRY = "llm-pi-ai"


def _split_at(text: str, marker: str) -> tuple[str, str]:
    """把 text 在 marker 之后切开（marker 属于前半部分）。"""
    index = text.index(marker)
    return text[: index + len(marker)], text[index + len(marker):]


def _keep(ids, occurrence_aware=False):
    """按 id 顺序生成 KeepItem 列表（重复 id 自动递增 occurrence）。"""
    counts: dict[str, int] = {}
    plan = []
    for model_id in ids:
        occurrence = counts.get(model_id, 0)
        counts[model_id] = occurrence + 1
        plan.append(yp.KeepItem(model_id, occurrence))
    return plan


def _plan_from(text: str, kind: str, route: str, *, add=(), drop=(), sort=False):
    """按现状生成 plan：可选丢弃若干 id、可选排序、可选追加 NewItem。"""
    ids = [i for i in yp.read_model_ids(text, kind, ENTRY, route) if i not in set(drop)]
    if sort:
        ids = sorted(ids)
    plan = _keep(ids)
    for fields in add:
        plan.append(yp.NewItem(fields))
    return plan


# --------------------------------------------------------------------------- #
# detect_kind
# --------------------------------------------------------------------------- #


class DetectKindTests(unittest.TestCase):
    def test_patch_root_is_list(self):
        self.assertEqual(yp.detect_kind(PATCH_TEXT), yp.KIND_PATCH)

    def test_settings_root_is_mapping(self):
        self.assertEqual(yp.detect_kind(SETTINGS_TEXT), yp.KIND_SETTINGS)

    def test_leading_comments_and_document_marker(self):
        text = "# 注释\n---\n- id: x\n  config:\n    a: 1\n"
        self.assertEqual(yp.detect_kind(text), yp.KIND_PATCH)

    def test_empty_text_raises(self):
        with self.assertRaises(yp.YamlPatchError):
            yp.detect_kind("")

    def test_comment_only_raises(self):
        with self.assertRaises(yp.YamlPatchError):
            yp.detect_kind("# 只有注释\n\n   \n")

    def test_unparsable_line_raises(self):
        with self.assertRaises(yp.YamlPatchError) as ctx:
            yp.detect_kind("这不是 YAML\n")
        self.assertIn("无法判定", str(ctx.exception))

    def test_empty_collection_raises(self):
        with self.assertRaises(yp.YamlPatchError):
            yp.detect_kind("[]\n")

    def test_kind_constants(self):
        self.assertEqual(yp.KIND_PATCH, "patch")
        self.assertEqual(yp.KIND_SETTINGS, "settings")


# --------------------------------------------------------------------------- #
# list_routes / read_route / read_model_ids
# --------------------------------------------------------------------------- #


class ListRoutesTests(unittest.TestCase):
    def test_patch_routes_in_order(self):
        self.assertEqual(yp.list_routes(PATCH_TEXT, yp.KIND_PATCH, ENTRY), ["agnesai", "iris"])

    def test_settings_routes(self):
        self.assertEqual(yp.list_routes(SETTINGS_TEXT, yp.KIND_SETTINGS, ENTRY), ["iris"])

    def test_unknown_entry_raises(self):
        with self.assertRaises(yp.YamlPatchError) as ctx:
            yp.list_routes(PATCH_TEXT, yp.KIND_PATCH, "no-such-entry")
        self.assertIn("未找到", str(ctx.exception))

    def test_kind_mismatch_raises(self):
        with self.assertRaises(yp.YamlPatchError) as ctx:
            yp.list_routes(PATCH_TEXT, yp.KIND_SETTINGS, ENTRY)
        self.assertIn("不一致", str(ctx.exception))

    def test_bad_kind_raises(self):
        with self.assertRaises(yp.YamlPatchError):
            yp.list_routes(PATCH_TEXT, "json", ENTRY)

    def test_entry_without_providers_raises(self):
        text = "- id: llm-pi-ai\n  config:\n    other: 1\n"
        with self.assertRaises(yp.YamlPatchError) as ctx:
            yp.list_routes(text, yp.KIND_PATCH, ENTRY)
        self.assertIn("providers", str(ctx.exception))


class ReadRouteTests(unittest.TestCase):
    def test_scalar_fields_patch(self):
        route = yp.read_route(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "agnesai")
        self.assertIsNotNone(route)
        self.assertEqual(route["displayName"], "AgnesAI")
        self.assertEqual(route["apiKeyEnv"], "AGNESAI_API_KEY")
        self.assertEqual(route["api"], "openai-completions")
        self.assertEqual(route["baseURL"], "https://api.vendor.example.com/v1")

    def test_missing_route_returns_none(self):
        self.assertIsNone(yp.read_route(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "nope"))

    def test_headers_and_models_are_parsed(self):
        route = yp.read_route(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "iris")
        self.assertEqual(
            route["headers"],
            {"X-Title": "dsh-byok-sync", "HTTP-Referer": "https://example.com/referer"},
        )
        self.assertEqual([m["id"] for m in route["models"]], ["deepseek-v4-flash", "glm-5.3-flash"])

    def test_nested_fields_parse_to_python_values(self):
        route = yp.read_route(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "agnesai")
        first = route["models"][0]
        self.assertEqual(first["contextWindow"], 512000)
        self.assertEqual(first["maxTokens"], 65536)
        self.assertEqual(first["input"], ["text", "image"])
        self.assertIsNone(first["reasoningEfforts"]["off"])
        self.assertEqual(first["reasoningEfforts"]["high"], "high")

    def test_settings_route(self):
        route = yp.read_route(SETTINGS_TEXT, yp.KIND_SETTINGS, ENTRY, "iris")
        self.assertEqual(route["baseURL"], "https://gateway.example.com/v1")
        self.assertEqual(len(route["models"]), 3)

    def test_flow_value_route(self):
        route = yp.read_route(FLOW_TEXT, yp.KIND_PATCH, ENTRY, "nvidia")
        self.assertEqual(route["models"], [{"id": "alpha"}, {"id": "beta"}])


class ReadModelIdsTests(unittest.TestCase):
    def test_block_ids(self):
        self.assertEqual(
            yp.read_model_ids(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "iris"),
            ["deepseek-v4-flash", "glm-5.3-flash"],
        )

    def test_quoted_ids_are_unquoted(self):
        ids = yp.read_model_ids(SETTINGS_TEXT, yp.KIND_SETTINGS, ENTRY, "iris")
        self.assertEqual(ids[0], "[free]claude-opus-5")

    def test_duplicate_ids_kept_in_order(self):
        ids = yp.read_model_ids(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "agnesai")
        self.assertEqual(ids.count("agnes-3.0-flash"), 2)
        self.assertEqual(ids[-1], "agnes-3.0-flash")

    def test_flow_empty_list(self):
        self.assertEqual(yp.read_model_ids(FLOW_TEXT, yp.KIND_PATCH, ENTRY, "iris"), [])

    def test_flow_mapping_list(self):
        self.assertEqual(
            yp.read_model_ids(FLOW_TEXT, yp.KIND_PATCH, ENTRY, "nvidia"), ["alpha", "beta"]
        )

    def test_missing_route_returns_empty(self):
        self.assertEqual(yp.read_model_ids(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "nope"), [])

    def test_items_without_id_are_skipped(self):
        text = (
            "- id: llm-pi-ai\n"
            "  config:\n"
            "    providers:\n"
            "      iris:\n"
            "        models:\n"
            "          - name: 没有 id\n"
            "          - id: good\n"
            "          - id: \"\"\n"
        )
        self.assertEqual(yp.read_model_ids(text, yp.KIND_PATCH, ENTRY, "iris"), ["good"])


# --------------------------------------------------------------------------- #
# apply_plan：no-op 与基本改写
# --------------------------------------------------------------------------- #


class ApplyPlanBasicsTests(unittest.TestCase):
    def test_noop_returns_input_text(self):
        for route in ("agnesai", "iris"):
            plan = _plan_from(PATCH_TEXT, yp.KIND_PATCH, route)
            self.assertEqual(yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, route, plan), PATCH_TEXT)

    def test_noop_settings(self):
        plan = _plan_from(SETTINGS_TEXT, yp.KIND_SETTINGS, "iris")
        self.assertEqual(
            yp.apply_plan(SETTINGS_TEXT, yp.KIND_SETTINGS, ENTRY, "iris", plan), SETTINGS_TEXT
        )

    def test_add_item_appends_and_leaves_rest_untouched(self):
        plan = _plan_from(
            PATCH_TEXT,
            yp.KIND_PATCH,
            "agnesai",
            add=[{"id": "agnes-4.0-flash", "name": "agnes-4.0-flash", "contextWindow": 1000000}],
        )
        out = yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "agnesai", plan)
        head, tail = _split_at(PATCH_TEXT, "        models:\n")
        _block, after = _split_at(tail, "      iris:\n")
        self.assertTrue(out.startswith(head), "models 块之前的字节必须不变")
        self.assertTrue(out.endswith("      iris:\n" + after), "models 块之后的字节必须不变")
        self.assertIn(
            "            name: agnes-3.0-flash (重复 id 副本)\n"
            "          - id: agnes-4.0-flash\n"
            "            name: agnes-4.0-flash\n"
            "            contextWindow: 1000000\n",
            out,
        )
        self.assertEqual(
            yp.read_model_ids(out, yp.KIND_PATCH, ENTRY, "agnesai")[-1], "agnes-4.0-flash"
        )

    def test_delete_item_removes_only_that_chunk(self):
        plan = _plan_from(PATCH_TEXT, yp.KIND_PATCH, "agnesai", drop={"agnes-2.0-flash"})
        out = yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "agnesai", plan)
        self.assertNotIn("agnes-2.0-flash", out)
        # 该条目自己的前导注释随它一起走
        self.assertNotIn("手工保留的免费通道", out)
        self.assertEqual(
            yp.read_model_ids(out, yp.KIND_PATCH, ENTRY, "agnesai"),
            ["agnes-3.0-flash", "[free]agnes-quoted", "agnes-3.0-flash"],
        )

    def test_sort_reorders_chunks(self):
        plan = _plan_from(PATCH_TEXT, yp.KIND_PATCH, "agnesai", sort=True)
        out = yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "agnesai", plan)
        self.assertEqual(
            yp.read_model_ids(out, yp.KIND_PATCH, ENTRY, "agnesai"),
            sorted(yp.read_model_ids(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "agnesai")),
        )

    def test_reorder_keeps_other_providers_byte_identical(self):
        plan = _plan_from(PATCH_TEXT, yp.KIND_PATCH, "agnesai", sort=True)
        out = yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "agnesai", plan)
        # iris 段的全部内容（含 headers 与模型列表）必须原样保留
        iris_block = PATCH_TEXT[PATCH_TEXT.index("      iris:"):]
        self.assertTrue(out.endswith(iris_block))

    def test_comment_and_blank_lines_preserved(self):
        text = (
            "- id: llm-pi-ai\n"
            "  config:\n"
            "    providers:\n"
            "      iris:\n"
            "        apiKeyEnv: IRIS_API_KEY\n"
            "        models:\n"
            "          # 顶部注释\n"
            "          - id: alpha\n"
            "            name: alpha\n"
            "\n"
            "          # beta 的注释\n"
            "          - id: beta\n"
            "            name: beta\n"
            "\n"
            "      other:\n"
            "        apiKeyEnv: OTHER\n"
        )
        plan = [yp.KeepItem("beta"), yp.KeepItem("alpha")]
        out = yp.apply_plan(text, yp.KIND_PATCH, ENTRY, "iris", plan)
        expected = (
            "- id: llm-pi-ai\n"
            "  config:\n"
            "    providers:\n"
            "      iris:\n"
            "        apiKeyEnv: IRIS_API_KEY\n"
            "        models:\n"
            "          # beta 的注释\n"
            "          - id: beta\n"
            "            name: beta\n"
            "\n"
            "          # 顶部注释\n"
            "          - id: alpha\n"
            "            name: alpha\n"
            "\n"
            "      other:\n"
            "        apiKeyEnv: OTHER\n"
        )
        self.assertEqual(out, expected)

    def test_kept_item_text_is_byte_identical(self):
        # 刻意用不常规但合法的写法（多行 plain scalar、行尾注释）确认原样搬运
        text = (
            "- id: llm-pi-ai\n"
            "  config:\n"
            "    providers:\n"
            "      iris:\n"
            "        models:\n"
            "          - id: alpha  # 行尾注释\n"
            "            name: alpha\n"
            "          - id: beta\n"
            "            description: 很长的一段说明，\n"
            "              被换行折行了\n"
        )
        plan = [yp.KeepItem("beta"), yp.KeepItem("alpha"), yp.NewItem({"id": "gamma"})]
        out = yp.apply_plan(text, yp.KIND_PATCH, ENTRY, "iris", plan)
        self.assertIn(
            "          - id: beta\n"
            "            description: 很长的一段说明，\n"
            "              被换行折行了\n",
            out,
        )
        self.assertIn("          - id: alpha  # 行尾注释\n            name: alpha\n", out)
        self.assertTrue(out.endswith("          - id: gamma\n"))

    def test_quoted_free_id_kept_verbatim(self):
        plan = _plan_from(SETTINGS_TEXT, yp.KIND_SETTINGS, "iris")
        out = yp.apply_plan(SETTINGS_TEXT, yp.KIND_SETTINGS, ENTRY, "iris", plan)
        self.assertIn('        - id: "[free]claude-opus-5"\n', out)
        # 挪动顺序也不改写引号
        plan2 = [yp.KeepItem("deepseek-v4-pro"), yp.KeepItem("[free]claude-opus-5")]
        out2 = yp.apply_plan(SETTINGS_TEXT, yp.KIND_SETTINGS, ENTRY, "iris", plan2)
        self.assertIn('        - id: "[free]claude-opus-5"\n          name: "[free]claude-opus-5"\n', out2)

    def test_duplicate_ids_resolved_by_occurrence(self):
        plan = [yp.KeepItem("agnes-3.0-flash", 1), yp.KeepItem("agnes-3.0-flash", 0)]
        out = yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "agnesai", plan)
        self.assertIn("(重复 id 副本)", out)
        self.assertLess(out.index("(重复 id 副本)"), out.index("maxTokens: 65536"))

    def test_settings_shape_new_item_indent(self):
        plan = _plan_from(
            SETTINGS_TEXT,
            yp.KIND_SETTINGS,
            "iris",
            add=[{"id": "new-one", "name": "New One"}],
        )
        out = yp.apply_plan(SETTINGS_TEXT, yp.KIND_SETTINGS, ENTRY, "iris", plan)
        self.assertIn(
            "        - id: deepseek-v4-pro\n"
            "          name: deepseek-v4-pro\n"
            "        - id: new-one\n"
            "          name: New One\n"
            "ui-theme:\n",
            out,
        )

    def test_provider_other_fields_untouched(self):
        plan = _plan_from(PATCH_TEXT, yp.KIND_PATCH, "agnesai", add=[{"id": "z"}])
        out = yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "agnesai", plan)
        for line in (
            "        displayName: AgnesAI\n",
            "        apiKeyEnv: AGNESAI_API_KEY\n",
            "        baseURL: https://api.vendor.example.com/v1\n",
        ):
            self.assertEqual(out.count(line), 1)
        # api 这一行两个路由都有，只确认 agnesai 段里仍在原位置
        self.assertIn(
            "        apiKeyEnv: AGNESAI_API_KEY\n        api: openai-completions\n"
            "        baseURL: https://api.vendor.example.com/v1\n        models:\n",
            out,
        )


# --------------------------------------------------------------------------- #
# apply_plan：换行 / 末尾换行 / 缩进
# --------------------------------------------------------------------------- #


class ApplyPlanNewlineTests(unittest.TestCase):
    def test_crlf_preserved(self):
        text = PATCH_TEXT.replace("\n", "\r\n")
        plan = _plan_from(text, yp.KIND_PATCH, "agnesai", add=[{"id": "agnes-4.0-flash"}])
        out = yp.apply_plan(text, yp.KIND_PATCH, ENTRY, "agnesai", plan)
        self.assertNotIn("\n", out.replace("\r\n", ""))
        self.assertIn("          - id: agnes-4.0-flash\r\n", out)
        self.assertTrue(out.endswith("\r\n"))

    def test_noop_keeps_crlf(self):
        text = PATCH_TEXT.replace("\n", "\r\n")
        plan = _plan_from(text, yp.KIND_PATCH, "agnesai")
        self.assertEqual(yp.apply_plan(text, yp.KIND_PATCH, ENTRY, "agnesai", plan), text)

    def test_missing_final_newline_preserved_on_noop(self):
        text = STEP4_TEXT.rstrip("\n")
        self.assertFalse(text.endswith("\n"))
        plan = _plan_from(text, yp.KIND_PATCH, "iris")
        self.assertEqual(yp.apply_plan(text, yp.KIND_PATCH, ENTRY, "iris", plan), text)

    def test_missing_final_newline_preserved_when_appending(self):
        text = STEP4_TEXT.rstrip("\n")
        plan = _plan_from(text, yp.KIND_PATCH, "iris", add=[{"id": "gamma", "name": "gamma"}])
        out = yp.apply_plan(text, yp.KIND_PATCH, ENTRY, "iris", plan)
        self.assertFalse(out.endswith("\n"))
        self.assertTrue(out.endswith("            - id: gamma\n              name: gamma"))

    def test_indent_step_four_inferred(self):
        plan = [yp.KeepItem("beta"), yp.NewItem({"id": "gamma", "name": "gamma"})]
        out = yp.apply_plan(STEP4_TEXT, yp.KIND_PATCH, ENTRY, "iris", plan)
        self.assertIn(
            "        models:\n"
            "            - id: beta\n"
            "              name: beta\n"
            "            - id: gamma\n"
            "              name: gamma\n",
            out,
        )

    def test_indent_step_two_inferred(self):
        plan = [yp.NewItem({"id": "gamma"})]
        out = yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "iris", plan)
        self.assertIn("        models:\n          - id: gamma\n", out)


# --------------------------------------------------------------------------- #
# apply_plan：缺 models 键与流风格
# --------------------------------------------------------------------------- #


class ApplyPlanStructureTests(unittest.TestCase):
    def test_missing_models_key_inserts_block(self):
        plan = [yp.NewItem({"id": "alpha", "name": "alpha"})]
        out = yp.apply_plan(NO_MODELS_TEXT, yp.KIND_PATCH, ENTRY, "iris", plan)
        self.assertTrue(
            out.startswith(
                "- id: llm-pi-ai\n"
                "  config:\n"
                "    providers:\n"
                "      iris:\n"
                "        displayName: Iris\n"
                "        apiKeyEnv: IRIS_API_KEY\n"
                "        api: openai-completions\n"
                "        baseURL: https://gateway.example.com/v1\n"
                "        models:\n"
                "          - id: alpha\n"
                "            name: alpha\n"
            ),
            out,
        )
        # 后续路由原样保留
        self.assertIn("      nvidia:\n        apiKeyEnv: NVIDIA_API_KEY\n        models:\n          - id: alpha\n", out)
        self.assertEqual(yp.read_model_ids(out, yp.KIND_PATCH, ENTRY, "iris"), ["alpha"])

    def test_missing_models_key_with_empty_plan_is_noop(self):
        self.assertEqual(yp.apply_plan(NO_MODELS_TEXT, yp.KIND_PATCH, ENTRY, "iris", []), NO_MODELS_TEXT)

    def test_flow_empty_list_becomes_block(self):
        plan = [yp.NewItem({"id": "alpha", "name": "alpha"})]
        out = yp.apply_plan(FLOW_TEXT, yp.KIND_PATCH, ENTRY, "iris", plan)
        self.assertIn(
            "      iris:\n        apiKeyEnv: IRIS_API_KEY\n        models:\n"
            "          - id: alpha\n            name: alpha\n      nvidia:\n",
            out,
        )
        self.assertEqual(yp.read_model_ids(out, yp.KIND_PATCH, ENTRY, "iris"), ["alpha"])

    def test_flow_with_items_overwritten(self):
        plan = [yp.NewItem({"id": "gamma"})]
        out = yp.apply_plan(FLOW_TEXT, yp.KIND_PATCH, ENTRY, "nvidia", plan)
        self.assertNotIn("[{id: alpha}, {id: beta}]", out)
        self.assertEqual(yp.read_model_ids(out, yp.KIND_PATCH, ENTRY, "nvidia"), ["gamma"])

    def test_flow_keep_item_rejected(self):
        with self.assertRaises(yp.YamlPatchError) as ctx:
            yp.apply_plan(FLOW_TEXT, yp.KIND_PATCH, ENTRY, "nvidia", [yp.KeepItem("alpha")])
        self.assertIn("流风格", str(ctx.exception))

    def test_flow_trailing_comment_survives_rewrite(self):
        out = yp.apply_plan(FLOW_TEXT, yp.KIND_PATCH, ENTRY, "gemini", [yp.NewItem({"id": "z"})])
        # 行尾注释连同它前面的空白一起保留
        self.assertIn("        models:  # 纯标量列表\n          - id: z\n", out)

    def test_flow_emptied_by_empty_plan(self):
        out = yp.apply_plan(FLOW_TEXT, yp.KIND_PATCH, ENTRY, "gemini", [])
        self.assertIn("        models: []  # 纯标量列表\n", out)

    def test_empty_models_value_gets_items(self):
        plan = [yp.NewItem({"id": "alpha"})]
        out = yp.apply_plan(FLOW_TEXT, yp.KIND_PATCH, ENTRY, "empty", plan)
        self.assertIn("      empty:\n        apiKeyEnv: EMPTY_API_KEY\n        models:\n          - id: alpha\n", out)

    def test_models_key_line_comment_preserved(self):
        text = (
            "- id: llm-pi-ai\n"
            "  config:\n"
            "    providers:\n"
            "      iris:\n"
            "        models:  # 我的模型\n"
            "          - id: alpha\n"
        )
        out = yp.apply_plan(text, yp.KIND_PATCH, ENTRY, "iris", [yp.KeepItem("alpha"), yp.NewItem({"id": "beta"})])
        self.assertIn("        models:  # 我的模型\n          - id: alpha\n          - id: beta\n", out)

    def test_field_after_models_is_untouched(self):
        """settings.yaml 里出现过 models 之后还有 apiKeyEnv 的顺序。"""
        text = (
            "llm-pi-ai:\n"
            "  providers:\n"
            "    nvidia:\n"
            "      models:\n"
            "        - id: alpha\n"
            "          name: alpha\n"
            "      apiKeyEnv: NVIDIA_API_KEY\n"
            "    other:\n"
            "      apiKeyEnv: OTHER\n"
        )
        out = yp.apply_plan(text, yp.KIND_SETTINGS, ENTRY, "nvidia", [yp.KeepItem("alpha"), yp.NewItem({"id": "beta"})])
        self.assertIn(
            "      models:\n        - id: alpha\n          name: alpha\n"
            "        - id: beta\n      apiKeyEnv: NVIDIA_API_KEY\n    other:\n",
            out,
        )
        self.assertEqual(yp.read_route(out, yp.KIND_SETTINGS, ENTRY, "nvidia")["apiKeyEnv"], "NVIDIA_API_KEY")

    def test_quoted_entry_id_in_patch_list(self):
        text = (
            '- id: "llm-pi-ai"\n'
            "  config:\n"
            "    providers:\n"
            "      iris:\n"
            "        models: []\n"
        )
        self.assertEqual(yp.list_routes(text, yp.KIND_PATCH, ENTRY), ["iris"])
        out = yp.apply_plan(text, yp.KIND_PATCH, ENTRY, "iris", [yp.NewItem({"id": "alpha"})])
        self.assertTrue(out.startswith('- id: "llm-pi-ai"\n'))

    def test_entry_matched_by_id_not_by_position(self):
        text = (
            "- id: other\n"
            "  config:\n"
            "    providers:\n"
            "      ghost:\n"
            "        models: []\n"
            "- id: llm-pi-ai\n"
            "  config:\n"
            "    providers:\n"
            "      iris:\n"
            "        models: []\n"
        )
        self.assertEqual(yp.list_routes(text, yp.KIND_PATCH, ENTRY), ["iris"])

    def test_delete_all_models_keeps_valid_empty_list(self):
        plan = _plan_from(PATCH_TEXT, yp.KIND_PATCH, "iris", drop={"deepseek-v4-flash", "glm-5.3-flash"})
        out = yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "iris", plan)
        self.assertIn("        models: []\n      iris:" if False else "        models: []\n- id: ui-theme\n", out)
        self.assertEqual(yp.read_model_ids(out, yp.KIND_PATCH, ENTRY, "iris"), [])
        self.assertEqual(yp.list_routes(out, yp.KIND_PATCH, ENTRY), ["agnesai", "iris"])

    def test_empty_plan_on_empty_models_is_noop(self):
        self.assertEqual(yp.apply_plan(FLOW_TEXT, yp.KIND_PATCH, ENTRY, "iris", []), FLOW_TEXT)


# --------------------------------------------------------------------------- #
# apply_plan：不安全输入
# --------------------------------------------------------------------------- #


class ApplyPlanErrorTests(unittest.TestCase):
    def test_unknown_route_raises(self):
        with self.assertRaises(yp.YamlPatchError) as ctx:
            yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "nope", [])
        self.assertIn("不存在", str(ctx.exception))
        self.assertIn("agnesai", str(ctx.exception))

    def test_unknown_entry_raises(self):
        with self.assertRaises(yp.YamlPatchError):
            yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, "no-entry", "iris", [])

    def test_unknown_keep_id_raises(self):
        with self.assertRaises(yp.YamlPatchError) as ctx:
            yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "iris", [yp.KeepItem("ghost")])
        self.assertIn("ghost", str(ctx.exception))

    def test_occurrence_out_of_range_raises(self):
        with self.assertRaises(yp.YamlPatchError) as ctx:
            yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "agnesai", [yp.KeepItem("agnes-3.0-flash", 5)])
        self.assertIn("只有 2 条", str(ctx.exception))

    def test_duplicate_keep_reference_raises(self):
        with self.assertRaises(yp.YamlPatchError) as ctx:
            yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "iris", [yp.KeepItem("glm-5.3-flash")] * 2)
        self.assertIn("重复引用", str(ctx.exception))

    def test_tab_indent_raises(self):
        text = "- id: llm-pi-ai\nconfig:\n\tproviders:\n\t\ta:\n"
        with self.assertRaises(yp.YamlPatchError) as ctx:
            yp.apply_plan(text, yp.KIND_PATCH, ENTRY, "a", [])
        self.assertIn("Tab", str(ctx.exception))

    def test_anchor_in_models_raises(self):
        text = (
            "- id: llm-pi-ai\n"
            "  config:\n"
            "    providers:\n"
            "      iris:\n"
            "        models:\n"
            "          - &keep\n"
            "            id: alpha\n"
        )
        with self.assertRaises(yp.YamlPatchError) as ctx:
            yp.apply_plan(text, yp.KIND_PATCH, ENTRY, "iris", [yp.KeepItem("alpha")])
        self.assertIn("锚点", str(ctx.exception))

    def test_alias_in_models_raises(self):
        text = (
            "- id: llm-pi-ai\n"
            "  config:\n"
            "    providers:\n"
            "      iris:\n"
            "        models:\n"
            "          - id: *alias\n"
        )
        with self.assertRaises(yp.YamlPatchError):
            yp.apply_plan(text, yp.KIND_PATCH, ENTRY, "iris", [])

    def test_js_tag_raises(self):
        text = (
            "- id: llm-pi-ai\n"
            "  config:\n"
            "    providers:\n"
            "      iris:\n"
            "        models: !!js process.env.MODELS\n"
        )
        with self.assertRaises(yp.YamlPatchError) as ctx:
            yp.apply_plan(text, yp.KIND_PATCH, ENTRY, "iris", [])
        self.assertIn("!!js", str(ctx.exception))

    def test_new_item_without_id_raises(self):
        with self.assertRaises(yp.YamlPatchError) as ctx:
            yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "iris", [yp.NewItem({"name": "x"})])
        self.assertIn("id", str(ctx.exception))

    def test_new_item_with_empty_id_raises(self):
        with self.assertRaises(yp.YamlPatchError):
            yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "iris", [yp.NewItem({"id": "  "})])

    def test_unknown_plan_element_raises(self):
        with self.assertRaises(yp.YamlPatchError) as ctx:
            yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "iris", [("iris", "x")])
        self.assertIn("未知元素", str(ctx.exception))

    def test_kind_mismatch_raises(self):
        with self.assertRaises(yp.YamlPatchError):
            yp.apply_plan(SETTINGS_TEXT, yp.KIND_PATCH, ENTRY, "iris", [])

    def test_duplicate_key_raises(self):
        text = (
            "- id: llm-pi-ai\n"
            "  config:\n"
            "    providers:\n"
            "      iris:\n"
            "        apiKeyEnv: A\n"
            "        apiKeyEnv: B\n"
            "        models:\n"
            "          - id: alpha\n"
        )
        with self.assertRaises(yp.YamlPatchError) as ctx:
            yp.apply_plan(text, yp.KIND_PATCH, ENTRY, "iris", [yp.KeepItem("alpha")])
        self.assertIn("重复", str(ctx.exception))

    def test_multi_document_raises(self):
        text = "- id: llm-pi-ai\n  config:\n    providers:\n      iris:\n        models: []\n---\n- id: x\n"
        with self.assertRaises(yp.YamlPatchError) as ctx:
            yp.apply_plan(text, yp.KIND_PATCH, ENTRY, "iris", [])
        self.assertIn("多文档", str(ctx.exception))

    def test_trailing_document_end_marker_allowed(self):
        text = "- id: llm-pi-ai\n  config:\n    providers:\n      iris:\n        models: []\n...\n"
        out = yp.apply_plan(text, yp.KIND_PATCH, ENTRY, "iris", [yp.NewItem({"id": "alpha"})])
        self.assertTrue(out.endswith("          - id: alpha\n...\n"))


# --------------------------------------------------------------------------- #
# 新条目渲染（SPEC §4）
# --------------------------------------------------------------------------- #


class RenderNewItemTests(unittest.TestCase):
    def _render(self, fields):
        plan = [yp.NewItem(fields)]
        out = yp.apply_plan(FLOW_TEXT, yp.KIND_PATCH, ENTRY, "iris", plan)
        head, _sep, rest = out.partition("        models:\n")
        block, _sep2, _tail = rest.partition("      nvidia:\n")
        self.assertTrue(head)
        return block

    def test_field_order_is_canonical(self):
        block = self._render(
            {
                "reasoningEfforts": {"off": None},
                "maxTokens": 100,
                "id": "m",
                "input": ["text"],
                "name": "M",
                "contextWindow": 1000,
            }
        )
        self.assertEqual(
            block,
            "          - id: m\n"
            "            name: M\n"
            "            contextWindow: 1000\n"
            "            maxTokens: 100\n"
            "            input: [text]\n"
            "            reasoningEfforts:\n"
            "              off: null\n",
        )

    def test_unsafe_scalars_are_quoted(self):
        for value in ("[free]claude-opus-5", "true", "null", "12", "1e3", "0x1f", "-dash",
                      "a: b", "trail #hash", " padded", "*star", "%pct", "@at", "`tick"):
            block = self._render({"id": value})
            line = block.splitlines()[0]
            self.assertTrue(line.startswith('          - id: "'), f"{value!r} 应该被加引号：{line}")

    def test_empty_string_value_is_quoted(self):
        block = self._render({"id": "m", "name": ""})
        self.assertEqual(block, '          - id: m\n            name: ""\n')

    def test_safe_scalars_stay_plain(self):
        for value in ("deepseek-v4-flash", "agnes-3.0-flash", "01-ai/yi-large",
                      "minimax/minimax-m3.1-flash:free", "google/gemma-3-12b-it"):
            block = self._render({"id": value})
            self.assertEqual(block.splitlines()[0], f"          - id: {value}")

    def test_quote_escapes(self):
        block = self._render({"id": '"lead'})
        self.assertEqual(block.splitlines()[0], '          - id: "\\"lead"')
        block = self._render({"id": "-back\\slash"})
        self.assertEqual(block.splitlines()[0], '          - id: "-back\\\\slash"')
        block = self._render({"id": "two\nlines"})
        self.assertEqual(block.splitlines()[0], '          - id: "two\\nlines"')

    def test_none_false_and_ints(self):
        block = self._render({"id": "m", "maxTokens": None, "compat": False})
        self.assertEqual(block, "          - id: m\n            maxTokens: null\n            compat: false\n")

    def test_list_rendered_as_flow(self):
        block = self._render({"id": "m", "input": ["text", "image"]})
        self.assertEqual(block, "          - id: m\n            input: [text, image]\n")

    def test_nested_mapping_rendered_as_block(self):
        block = self._render(
            {"id": "m", "reasoningEfforts": {"off": None, "high": "high"}}
        )
        self.assertEqual(
            block,
            "          - id: m\n"
            "            reasoningEfforts:\n"
            "              off: null\n"
            "              high: high\n",
        )

    def test_extra_keys_appended_after_canonical(self):
        block = self._render({"id": "m", "compat": {"x": 1}, "zzz": 3})
        lines = block.splitlines()
        self.assertEqual(lines[0], "          - id: m")
        self.assertEqual(lines[1], "            compat:")
        self.assertEqual(lines[-1], "            zzz: 3")


# --------------------------------------------------------------------------- #
# 真实样例片段回归（与 cordis.patch.yml / settings.yaml 同构）
# --------------------------------------------------------------------------- #


class RealSampleRegressionTests(unittest.TestCase):
    """对“与真实文件同构”的片段做新增/删除/排序，断言区域外逐字节不变。"""

    def _assert_only_models_changed(self, text, out, route, *, models_marker, next_marker):
        head, tail = _split_at(text, models_marker)
        _block, after = _split_at(tail, next_marker)
        self.assertTrue(out.startswith(head), f"{route}: models 之前的内容被改动了")
        self.assertTrue(out.endswith(next_marker + after), f"{route}: models 之后的内容被改动了")

    def test_patch_route_agnesai_add(self):
        plan = _plan_from(
            PATCH_TEXT,
            yp.KIND_PATCH,
            "agnesai",
            add=[{"id": "agnes-4.0-flash", "name": "agnes-4.0-flash"}],
        )
        out = yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "agnesai", plan)
        self._assert_only_models_changed(
            PATCH_TEXT, out, "agnesai", models_marker="        models:\n", next_marker="      iris:\n"
        )
        self.assertIn("# 手工保留的免费通道\n", out)
        self.assertIn('          - id: "[free]agnes-quoted"\n', out)

    def test_patch_route_agnesai_delete(self):
        plan = _plan_from(PATCH_TEXT, yp.KIND_PATCH, "agnesai", drop={"[free]agnes-quoted"})
        out = yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "agnesai", plan)
        self._assert_only_models_changed(
            PATCH_TEXT, out, "agnesai", models_marker="        models:\n", next_marker="      iris:\n"
        )
        self.assertNotIn("agnes-quoted", out)

    def test_patch_route_agnesai_sort(self):
        plan = _plan_from(PATCH_TEXT, yp.KIND_PATCH, "agnesai", sort=True)
        out = yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "agnesai", plan)
        self._assert_only_models_changed(
            PATCH_TEXT, out, "agnesai", models_marker="        models:\n", next_marker="      iris:\n"
        )
        ids = yp.read_model_ids(out, yp.KIND_PATCH, ENTRY, "agnesai")
        self.assertEqual(ids, sorted(ids))

    def test_patch_route_iris_untouched_when_editing_agnesai(self):
        plan = _plan_from(PATCH_TEXT, yp.KIND_PATCH, "agnesai", sort=True)
        out = yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "agnesai", plan)
        iris_start = PATCH_TEXT.index("      iris:")
        self.assertIn(PATCH_TEXT[iris_start:], out)

    def test_other_patch_entries_untouched(self):
        plan = _plan_from(PATCH_TEXT, yp.KIND_PATCH, "iris", add=[{"id": "new-x"}])
        out = yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "iris", plan)
        prefix = PATCH_TEXT[: PATCH_TEXT.index("- id: llm-pi-ai\n")]
        suffix = PATCH_TEXT[PATCH_TEXT.index("- id: ui-theme\n"):]
        self.assertTrue(out.startswith(prefix))
        self.assertTrue(out.endswith(suffix))

    def test_settings_route_regression(self):
        plan = _plan_from(
            SETTINGS_TEXT,
            yp.KIND_SETTINGS,
            "iris",
            add=[{"id": "deepseek-v4.1-flash", "name": "deepseek-v4.1-flash", "contextWindow": 1000000}],
        )
        out = yp.apply_plan(SETTINGS_TEXT, yp.KIND_SETTINGS, ENTRY, "iris", plan)
        head, tail = _split_at(SETTINGS_TEXT, "      models:\n")
        _block, after = _split_at(tail, "ui-theme:\n")
        self.assertTrue(out.startswith(head))
        self.assertTrue(out.endswith("ui-theme:\n" + after))
        self.assertEqual(
            yp.read_model_ids(out, yp.KIND_SETTINGS, ENTRY, "iris"),
            ["[free]claude-opus-5", "deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4.1-flash"],
        )

    def test_many_round_trips_stay_stable(self):
        """连续两次应用等价 plan 必须稳定（幂等）。"""
        plan = _plan_from(PATCH_TEXT, yp.KIND_PATCH, "agnesai", sort=True, add=[{"id": "zzz"}])
        once = yp.apply_plan(PATCH_TEXT, yp.KIND_PATCH, ENTRY, "agnesai", plan)
        plan2 = _plan_from(once, yp.KIND_PATCH, "agnesai")
        self.assertEqual(yp.apply_plan(once, yp.KIND_PATCH, ENTRY, "agnesai", plan2), once)

    def test_chained_route_edits_accumulate(self):
        """sync.py 会按路由串行改写同一份文本，后一次必须看到前一次的结果。"""
        step1 = yp.apply_plan(
            PATCH_TEXT,
            yp.KIND_PATCH,
            ENTRY,
            "agnesai",
            _plan_from(PATCH_TEXT, yp.KIND_PATCH, "agnesai", add=[{"id": "agnes-4.0-flash"}]),
        )
        self.assertNotEqual(step1, PATCH_TEXT)
        step2 = yp.apply_plan(
            step1,
            yp.KIND_PATCH,
            ENTRY,
            "iris",
            _plan_from(step1, yp.KIND_PATCH, "iris", add=[{"id": "deepseek-v4.1-flash"}]),
        )
        self.assertEqual(
            yp.read_model_ids(step2, yp.KIND_PATCH, ENTRY, "agnesai")[-1], "agnes-4.0-flash"
        )
        self.assertEqual(
            yp.read_model_ids(step2, yp.KIND_PATCH, ENTRY, "iris")[-1], "deepseek-v4.1-flash"
        )
        # 两个无关插件条目仍然逐字节不变
        self.assertTrue(step2.startswith(PATCH_TEXT[: PATCH_TEXT.index("- id: llm-pi-ai\n")]))
        self.assertTrue(step2.endswith(PATCH_TEXT[PATCH_TEXT.index("- id: ui-theme\n"):]))



class ModelsStyleTest(unittest.TestCase):
    """``read_models_style`` 必须把每种写法如实分类，供 configfile 拒绝改写。"""

    def _style(self, models_block: str) -> str:
        text = (
            "- id: llm-pi-ai\n"
            "  config:\n"
            "    providers:\n"
            "      iris:\n"
            "        baseURL: https://x.test/v1\n"
            f"{models_block}"
        )
        return yp.read_models_style(text, yp.KIND_PATCH, ENTRY, "iris")

    def test_block_sequence(self):
        self.assertEqual("seq", self._style("        models:\n          - id: a\n"))

    def test_non_empty_flow(self):
        self.assertEqual("flow", self._style("        models: [a, b]\n"))

    def test_empty_flow(self):
        self.assertEqual("empty-list", self._style("        models: []\n"))

    def test_null_value(self):
        self.assertEqual("null", self._style("        models:\n"))

    def test_null_with_comment(self):
        self.assertEqual("null", self._style("        models:  # 待同步\n"))

    def test_block_scalar_is_not_null(self):
        """``models: |`` 是有内容的标量，绝不能被当成空列表覆盖。"""
        self.assertEqual("scalar", self._style("        models: |\n          - id: a\n"))

    def test_folded_scalar(self):
        self.assertEqual("scalar", self._style("        models: >\n          - id: a\n"))

    def test_plain_scalar(self):
        self.assertEqual("scalar", self._style("        models: oops\n"))

    def test_block_mapping(self):
        self.assertEqual("map", self._style("        models:\n          a: 1\n"))

    def test_missing_key(self):
        self.assertIsNone(self._style("        api: openai-completions\n"))

    def test_missing_route(self):
        text = "- id: llm-pi-ai\n  config:\n    providers:\n      iris:\n        models: []\n"
        self.assertIsNone(yp.read_models_style(text, yp.KIND_PATCH, ENTRY, "ghost"))

    def test_detect_kind_ignores_leading_bom(self):
        """带 UTF-8 BOM 的文本也要判定为 patch 形态。"""
        self.assertEqual(yp.KIND_PATCH, yp.detect_kind("\ufeff- id: llm-pi-ai\n"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
