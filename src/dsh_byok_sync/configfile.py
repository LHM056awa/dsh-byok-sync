"""Locate, read, and atomically write the DSH configuration file being synced."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import yamlpatch
from .models import ModelSyncError

__all__ = [
    "DEFAULT_ENTRY",
    "Document",
    "RouteConfig",
    "dsh_home",
    "load_document",
    "resolve_config",
    "write_text_atomic",
]

#: The plugin row (patch form) or top-level key (settings form) holding routes.
DEFAULT_ENTRY = "llm-pi-ai"

_BOM = "\ufeff"


@dataclass(frozen=True)
class RouteConfig:
    """One ``providers`` entry, as configured today."""

    route: str
    api: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    headers: dict = field(default_factory=dict)
    models: tuple = ()
    present: bool = True
    models_error: str | None = None


@dataclass(frozen=True)
class Document:
    """A parsed configuration file plus everything needed to write it back."""

    path: Path
    text: str
    kind: str
    entry_id: str
    newline: str
    bom: bool
    routes: dict


def dsh_home(environ: dict | None = None) -> Path:
    """Return the DSH home directory (``~/.dsh`` unless overridden)."""
    env = os.environ if environ is None else environ
    override = env.get("DSH_HOME") or env.get("DSH_PROFILE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".dsh"


def _profile_dir(profile: str | None, environ: dict) -> Path | None:
    explicit = environ.get("DSH_PROFILE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    name = profile or environ.get("DSH_PROFILE") or "desktop"
    return dsh_home(environ) / "profiles" / name


def resolve_config(
    explicit: str | None = None,
    profile: str | None = None,
    *,
    environ: dict | None = None,
) -> Path:
    """Return the configuration file to sync.

    Order: an explicit path, ``$DSH_PROFILE_DIR/cordis.patch.yml``, the named
    profile's ``cordis.patch.yml``, then ``~/.dsh/settings.yaml``.
    """
    env = os.environ if environ is None else environ

    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ModelSyncError(f"配置文件不存在：{path}")
        return path

    candidates: list[Path] = []
    profile_dir = _profile_dir(profile, env)
    if profile_dir is not None:
        candidates.append(profile_dir / "cordis.patch.yml")
    candidates.append(dsh_home(env) / "settings.yaml")

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    searched = "、".join(str(path) for path in candidates)
    raise ModelSyncError(f"找不到 DSH 配置文件，已尝试：{searched}。请用 --config 指定。")


def _decode(raw: bytes, path: Path) -> tuple[str, bool]:
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ModelSyncError(f"{path} 不是 UTF-8 编码的文本文件：{exc}") from exc
    return text, bom


def _normalize_models(models: object) -> tuple[tuple, str | None]:
    """Return the configured entries plus an error when the value is unusable.

    Entries that are not mappings carry no id and are therefore discarded by
    the sync engine; a non-list value (a mapping keyed by id, a block scalar)
    cannot be rewritten safely and is reported rather than guessed at.
    """
    if models is None:
        return (), None
    if not isinstance(models, (list, tuple)):
        return (), "models 不是列表，无法安全同步"
    return tuple(models), None


_MODELS_STYLE_ERRORS = {
    "flow": "models 是流风格（[...]），本工具不做有损改写；请先把它改成块风格再同步",
    "scalar": "models 是标量值（例如块标量 models: |），无法当作模型列表同步；请改成块序列",
    "map": "models 是块状映射，无法当作模型列表同步；请改成块序列",
}


def _route_config(route: str, fields: dict, models_style: str | None = None) -> RouteConfig:
    headers = fields.get("headers")
    if not isinstance(headers, dict):
        headers = {}
    models, models_error = _normalize_models(fields.get("models"))
    style_error = _MODELS_STYLE_ERRORS.get(models_style or "")
    if style_error is not None:
        models_error = style_error
    if models_error is not None:
        # An unusable value yields no usable list: never hand it to the engine.
        models = ()
    return RouteConfig(
        route=route,
        api=fields.get("api") if isinstance(fields.get("api"), str) else None,
        base_url=fields.get("baseURL") if isinstance(fields.get("baseURL"), str) else None,
        api_key_env=fields.get("apiKeyEnv") if isinstance(fields.get("apiKeyEnv"), str) else None,
        headers={str(k): str(v) for k, v in headers.items()},
        models=models,
        models_error=models_error,
    )


def load_document(path: str | Path, entry: str = DEFAULT_ENTRY) -> Document:
    """Read ``path`` into a :class:`Document`."""
    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise ModelSyncError(f"无法读取配置文件 {target}：{exc}") from exc

    text, bom = _decode(raw, target)
    newline = "\r\n" if "\r\n" in text else "\n"

    try:
        kind = yamlpatch.detect_kind(text)
        route_names = yamlpatch.list_routes(text, kind, entry)
        routes = {}
        for route in route_names:
            fields = yamlpatch.read_route(text, kind, entry, route) or {}
            if not isinstance(fields, dict):
                fields = {}
            style = yamlpatch.read_models_style(text, kind, entry, route)
            routes[route] = _route_config(route, fields, style)
    except yamlpatch.YamlPatchError as exc:
        raise ModelSyncError(f"{target}：{exc}") from exc

    if not routes:
        raise ModelSyncError(
            f"{target} 中没有找到 {entry} 的任何 providers 路由（entry 是否正确？可用 --entry 指定）"
        )

    return Document(
        path=target,
        text=text,
        kind=kind,
        entry_id=entry,
        newline=newline,
        bom=bom,
        routes=routes,
    )


def write_text_atomic(path: str | Path, text: str, *, bom: bool = False) -> None:
    """Write ``text`` beside ``path`` and replace it in one step."""
    target = Path(path)
    payload = (_BOM + text).encode("utf-8") if bom else text.encode("utf-8")
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, target)
    except OSError as exc:
        raise ModelSyncError(f"无法写入配置文件 {target}：{exc}") from exc
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
