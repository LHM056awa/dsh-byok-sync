"""Built-in model presets.

``presets.json`` ships next to this module and provides non-generic defaults
for model families whose real capacities a bare ``/v1/models`` reply does not
describe. Presets apply to **newly added** models only; models that already
exist in the configuration keep their configured values untouched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .models import MODALITIES, THINKING_LEVELS, ModelSyncError

__all__ = ["PRESETS_PATH", "Preset", "apply_presets", "load_presets"]

PRESETS_PATH = Path(__file__).resolve().parent / "presets.json"

#: Fields a preset may override. ``id`` identifies the entry and the endpoint
#: belongs to the provider route, so neither is a preset's business; any other
#: key is refused instead of being silently dropped.
_ALLOWED_OVERRIDE_KEYS = frozenset(
    {"name", "contextWindow", "maxTokens", "input", "reasoningEfforts"}
)


@dataclass(frozen=True)
class Preset:
    """One ``model_id_contains`` → ``overrides`` rule."""

    model_id_contains: tuple[str, ...]
    overrides: dict

    def matches(self, model_id: str) -> bool:
        lowered = model_id.lower()
        return any(fragment.lower() in lowered for fragment in self.model_id_contains)


def _fail(message: str) -> "ModelSyncError":
    return ModelSyncError(f"内置预设 presets.json 不合法：{message}")


def _validate_override(model_id_contains: list[str], key: str, value, index: int) -> None:
    where = f"presets[{index}]（匹配 {model_id_contains}）的 {key}"
    if key == "name":
        if not isinstance(value, str) or not value.strip():
            raise _fail(f"{where} 必须是非空字符串")
        return
    if key in ("contextWindow", "maxTokens"):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise _fail(f"{where} 必须是正整数")
        return
    if key == "input":
        if not isinstance(value, list) or not value:
            raise _fail(f"{where} 必须是非空列表")
        for modality in value:
            if modality not in MODALITIES:
                raise _fail(f"{where} 含未知模态 {modality!r}，只允许 {list(MODALITIES)}")
        return
    if key == "reasoningEfforts":
        if value is False:
            return
        if not isinstance(value, dict):
            raise _fail(f"{where} 必须是 false 或 映射")
        if not value:
            raise _fail(f"{where} 不能为空映射")
        for level, spelling in value.items():
            if level not in THINKING_LEVELS:
                raise _fail(f"{where} 含未知档位 {level!r}，只允许 {list(THINKING_LEVELS)}")
            if spelling is not None and not isinstance(spelling, str):
                raise _fail(f"{where} 的 {level} 必须是字符串或 null")
        return
    raise _fail(f"{where} 不在允许覆盖的字段 {sorted(_ALLOWED_OVERRIDE_KEYS)} 中")


def load_presets(path: Path | None = None) -> tuple[Preset, ...]:
    """Load and validate the built-in presets.

    Raises :class:`ModelSyncError` on a missing, corrupt, or invalid file: the
    tool never silently degrades to "no presets".
    """
    target = Path(path) if path is not None else PRESETS_PATH
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise _fail(f"无法读取 {target}：{exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _fail(f"{target} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise _fail("顶层必须是对象")
    entries = data.get("presets")
    if not isinstance(entries, list):
        raise _fail("顶层 presets 必须是数组")

    presets: list[Preset] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise _fail(f"presets[{index}] 必须是对象")
        unknown = set(entry) - {"model_id_contains", "overrides"}
        if unknown:
            raise _fail(f"presets[{index}] 含未知字段 {sorted(unknown)}")
        contains = entry.get("model_id_contains")
        if not isinstance(contains, list) or not contains:
            raise _fail(f"presets[{index}].model_id_contains 必须是非空字符串数组")
        for fragment in contains:
            if not isinstance(fragment, str) or not fragment:
                raise _fail(f"presets[{index}].model_id_contains 的元素必须是非空字符串")
        overrides = entry.get("overrides")
        if not isinstance(overrides, dict):
            raise _fail(f"presets[{index}].overrides 必须是对象")
        if not overrides:
            raise _fail(f"presets[{index}].overrides 不能为空对象")
        for key, value in overrides.items():
            if key not in _ALLOWED_OVERRIDE_KEYS:
                raise _fail(
                    f"presets[{index}].overrides 的 {key} 不被允许（只允许 "
                    f"{sorted(_ALLOWED_OVERRIDE_KEYS)}）"
                )
            _validate_override(contains, key, value, index)
        presets.append(Preset(tuple(contains), dict(overrides)))
    return tuple(presets)


def apply_presets(model_id: str, presets: tuple[Preset, ...]) -> dict:
    """Return the overrides of the first matching preset, or ``{}``."""
    for preset in presets:
        if preset.matches(model_id):
            return dict(preset.overrides)
    return {}
