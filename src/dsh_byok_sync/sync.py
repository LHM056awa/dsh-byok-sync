"""Core synchronization: diff the configured model list against each endpoint."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Iterable, Sequence

from . import yamlpatch
from .client import (
    build_auth_headers,
    fetch_credits,
    fetch_models,
    known_credit_host,
    normalize_models_url,
)
from .credentials import Credentials
from .models import (
    FetchResult,
    ModelSyncError,
    RemoteModel,
    base_display_name,
    is_non_text_model,
)
from .presets import Preset, apply_presets, load_presets

__all__ = [
    "DEFAULT_TIMEOUT",
    "RouteReport",
    "SyncOptions",
    "SyncOutcome",
    "sync_config",
]

DEFAULT_TIMEOUT = 15.0

#: Field order new model entries are rendered with.
FIELD_ORDER: tuple[str, ...] = (
    "id",
    "name",
    "contextWindow",
    "maxTokens",
    "input",
    "reasoningEfforts",
)


@dataclass(frozen=True)
class SyncOptions:
    """Everything the sync engine needs beyond the document itself."""

    no_delete: bool = False
    sort: bool = False
    timeout: float = DEFAULT_TIMEOUT
    use_credits: bool = True
    credentials: Credentials | None = None
    presets: tuple[Preset, ...] | None = None
    fetch: Callable[..., FetchResult] | None = None
    credits_fetch: Callable[..., str | None] | None = None


@dataclass(frozen=True)
class RouteReport:
    """What happened to one route."""

    route: str
    url: str | None = None
    error: str | None = None
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    kept: int = 0
    filtered: tuple[str, ...] = ()
    no_text_signal: bool = False
    deletion_skipped: str | None = None
    discarded: tuple[str, ...] = ()
    credits: str | None = None
    changed: bool = False

    @property
    def unchanged(self) -> bool:
        return not self.changed


@dataclass(frozen=True)
class SyncOutcome:
    """The reports plus the full new file text (``None`` when nothing changed)."""

    reports: tuple[RouteReport, ...]
    new_text: str | None = None

    @property
    def changed(self) -> bool:
        return any(report.changed for report in self.reports)

    @property
    def failed(self) -> tuple[RouteReport, ...]:
        return tuple(report for report in self.reports if report.error)


def _describe_entry(entry: object, index: int) -> str:
    if isinstance(entry, dict):
        keys = ", ".join(str(key) for key in entry) or "空映射"
        return f"#{index + 1} 缺少有效 id（字段：{keys}）"
    return f"#{index + 1} 不是映射（{type(entry).__name__}）"


def _split_local(models: Iterable[object]) -> tuple[list[tuple[str, dict]], list[str]]:
    """Split configured entries into (valid id, entry) pairs and discarded ones."""
    valid: list[tuple[str, dict]] = []
    discarded: list[str] = []
    for index, entry in enumerate(models):
        if isinstance(entry, dict):
            raw_id = entry.get("id")
            if isinstance(raw_id, str) and raw_id.strip():
                valid.append((raw_id, entry))
                continue
        discarded.append(_describe_entry(entry, index))
    return valid, discarded


def _keep_all(local_valid: Sequence[tuple[str, dict]]) -> list:
    """A plan that keeps every valid configured entry in place."""
    plan: list = []
    occurrence: dict[str, int] = {}
    for model_id, _entry in local_valid:
        plan.append(yamlpatch.KeepItem(model_id, occurrence.get(model_id, 0)))
        occurrence[model_id] = occurrence.get(model_id, 0) + 1
    return plan


def _remote_index(result: FetchResult) -> tuple[list[str], dict[str, list[RemoteModel]]]:
    """Return remote ids in first-seen order plus every entry per id."""
    order: list[str] = []
    by_id: dict[str, list[RemoteModel]] = {}
    for model in result.models:
        if not isinstance(model.id, str) or not model.id.strip():
            continue
        if model.id not in by_id:
            by_id[model.id] = []
            order.append(model.id)
        by_id[model.id].append(model)
    return order, by_id


def _is_text(entries: Sequence[RemoteModel]) -> bool:
    """One text entry is enough to treat the whole id as a text model."""
    return any(not is_non_text_model(entry.id, entry.name) for entry in entries)


def _plan_id(item) -> str:
    """Sort key for a plan item, whether it keeps an entry or adds one."""
    if isinstance(item, yamlpatch.KeepItem):
        return item.id
    return item.fields["id"]


def _new_entry_fields(model: RemoteModel, overrides: dict) -> dict:
    """Build the field mapping for one newly added model entry."""
    fields: dict = {"id": model.id}

    preset_name = overrides.get("name")
    if isinstance(preset_name, str) and preset_name.strip():
        fields["name"] = preset_name
    else:
        fields["name"] = base_display_name(model)

    context = (
        model.contextWindow if model.contextWindow is not None else overrides.get("contextWindow")
    )
    if isinstance(context, int) and context > 0:
        fields["contextWindow"] = context

    max_tokens = (
        model.maxTokens if model.maxTokens is not None else overrides.get("maxTokens")
    )
    if isinstance(max_tokens, int) and max_tokens > 0:
        fields["maxTokens"] = max_tokens

    if "input" in overrides:
        fields["input"] = list(overrides["input"])
    if "reasoningEfforts" in overrides:
        fields["reasoningEfforts"] = overrides["reasoningEfforts"]

    return {key: fields[key] for key in FIELD_ORDER if key in fields}


def _plan_for_route(
    route_config,
    result: FetchResult,
    options: SyncOptions,
    presets: tuple[Preset, ...],
) -> tuple[RouteReport, list | None]:
    """Compute the edit plan and report for one route."""
    local_valid, discarded = _split_local(route_config.models)
    local_ids = {model_id for model_id, _ in local_valid}

    if not result.ok:
        # A failed request authorizes no model placement decision, but entries
        # without an id are meaningless in every mode and are still cleaned.
        return (
            RouteReport(
                route=route_config.route,
                url=result.url,
                error=result.error,
                kept=len(local_valid),
                discarded=tuple(discarded),
            ),
            _keep_all(local_valid) if discarded else None,
            local_valid,
        )

    order, by_id = _remote_index(result)
    text_ids = {model_id for model_id in order if _is_text(by_id[model_id])}
    text_signal = bool(text_ids)
    stale = [model_id for model_id, _entry in local_valid if model_id not in by_id]

    deletion_allowed = not options.no_delete and text_signal
    if options.no_delete:
        skip_reason = "deletion disabled by --no-delete"
    elif not text_signal:
        skip_reason = "no text signal"
    else:
        skip_reason = None

    plan: list = []
    added: list[str] = []
    removed: list[str] = []
    occurrence: dict[str, int] = {}
    kept = 0
    deletion_skipped: str | None = None

    for model_id, _entry in local_valid:
        if model_id in by_id:
            plan.append(yamlpatch.KeepItem(model_id, occurrence.get(model_id, 0)))
            occurrence[model_id] = occurrence.get(model_id, 0) + 1
            kept += 1
        elif deletion_allowed:
            removed.append(model_id)
        else:
            plan.append(yamlpatch.KeepItem(model_id, occurrence.get(model_id, 0)))
            occurrence[model_id] = occurrence.get(model_id, 0) + 1
            kept += 1
            deletion_skipped = deletion_skipped or skip_reason

    filtered: list[str] = []
    for model_id in order:
        if model_id in text_ids:
            if model_id in local_ids:
                continue
            model = by_id[model_id][0]
            overrides = apply_presets(model_id, presets)
            plan.append(yamlpatch.NewItem(_new_entry_fields(model, overrides)))
            added.append(model_id)
        elif model_id not in local_ids:
            filtered.append(model_id)

    if options.sort:
        plan.sort(key=_plan_id)

    return (
        RouteReport(
            route=route_config.route,
            url=result.url,
            added=tuple(added),
            removed=tuple(removed),
            kept=kept,
            filtered=tuple(filtered),
            no_text_signal=bool(stale) and not text_signal and not options.no_delete,
            deletion_skipped=deletion_skipped,
            discarded=tuple(discarded),
        ),
        plan,
        local_valid,
    )


def _plan_is_identity(plan: Sequence, local_valid: Sequence[tuple[str, dict]], total: int) -> bool:
    """Whether the plan keeps every configured entry in its current order.

    ``total`` is the number of configured entries *before* invalid ones were
    dropped, so a plan that silently skips a discarded entry is not identity.
    """
    if len(local_valid) != total:
        return False
    if len(plan) != len(local_valid):
        return False
    for item, (model_id, _entry) in zip(plan, local_valid):
        if not isinstance(item, yamlpatch.KeepItem):
            return False
        if item.id != model_id:
            return False
    return True


def _apply_plan(text: str, document, route: str, plan) -> str:
    """Apply one route's plan, turning an unsafe-edit refusal into a sync error."""
    try:
        return yamlpatch.apply_plan(text, document.kind, document.entry_id, route, plan)
    except yamlpatch.YamlPatchError as exc:
        raise ModelSyncError(f"路由 {route} 无法安全改写：{exc}") from exc


def sync_config(
    document,
    routes: Sequence[str] | None = None,
    *,
    options: SyncOptions | None = None,
    no_delete: bool = False,
    sort: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    use_credits: bool = True,
    credentials: Credentials | None = None,
    fetch: Callable[..., FetchResult] | None = None,
    credits_fetch: Callable[..., str | None] | None = None,
    presets: tuple[Preset, ...] | None = None,
) -> SyncOutcome:
    """Sync the selected routes and return the reports plus the new file text.

    ``new_text`` is ``None`` when nothing substantive changed, which is what
    keeps a no-op run from rewriting the file.
    """
    if options is None:
        options = SyncOptions(
            no_delete=no_delete,
            sort=sort,
            timeout=timeout,
            use_credits=use_credits,
            credentials=credentials,
            presets=presets,
            fetch=fetch,
            credits_fetch=credits_fetch,
        )

    selected = list(routes) if routes is not None else list(document.routes)
    unknown = [route for route in selected if route not in document.routes]
    if unknown:
        raise ModelSyncError(
            f"未知路由 {unknown}；可选：{sorted(document.routes)}"
        )

    fetcher = options.fetch or fetch_models
    credits_fetcher = options.credits_fetch or fetch_credits
    presets = options.presets if options.presets is not None else load_presets()
    credentials = options.credentials if options.credentials is not None else Credentials.load()

    text = document.text
    reports: list[RouteReport] = []

    for route in selected:
        route_config = document.routes[route]
        base_url = (route_config.base_url or "").strip()
        api = (route_config.api or "openai-completions").strip() or "openai-completions"

        if route_config.models_error:
            reports.append(RouteReport(route=route, error=route_config.models_error))
            continue

        if not base_url:
            local_valid, discarded = _split_local(route_config.models)
            reports.append(
                RouteReport(
                    route=route,
                    error="no endpoint url",
                    kept=len(local_valid),
                    discarded=tuple(discarded),
                )
            )
            # No endpoint means no placement decision, but id-less entries
            # remain meaningless and are dropped in every mode.
            if discarded:
                updated = _apply_plan(text, document, route, _keep_all(local_valid))
                if updated != text:
                    reports[-1] = replace(reports[-1], changed=True)
                    text = updated
            continue

        url = normalize_models_url(base_url, api)
        api_key = credentials.resolve(route_config.api_key_env, route)
        headers = build_auth_headers(api, api_key, route_config.headers)
        result = fetcher(url, headers, options.timeout)

        report, plan, local_valid = _plan_for_route(route_config, result, options, presets)

        if options.use_credits:
            host = known_credit_host(base_url)
            if host is not None:
                value = credits_fetcher(base_url, api_key, headers, options.timeout)
                report = replace(report, credits=value or "unavailable")

        if plan is not None and not _plan_is_identity(
            plan, local_valid, len(route_config.models)
        ):
            updated = _apply_plan(text, document, route, plan)
            if updated != text:
                report = replace(report, changed=True)
                text = updated

        reports.append(report)

    return SyncOutcome(tuple(reports), None if text == document.text else text)
