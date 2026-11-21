"""Data structures, constants, and the non-text model filter.

Everything here is provider-neutral: the DSH-specific wire rules live in
:mod:`dsh_byok_sync.client`, the file editing in :mod:`dsh_byok_sync.yamlpatch`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "MODALITIES",
    "THINKING_LEVELS",
    "FetchResult",
    "ModelSyncError",
    "RemoteModel",
    "base_display_name",
    "is_non_text",
    "is_non_text_model",
]


class ModelSyncError(Exception):
    """Raised when the configuration cannot be read, validated, or written."""


#: Request modalities a DSH ``llm-pi-ai`` model entry may declare.
MODALITIES: tuple[str, ...] = ("text", "image")

#: Selectable reasoning levels a DSH ``llm-pi-ai`` model entry may declare,
#: from least to greatest effort (mirrors ``catalog.ts`` ``THINKING_LEVELS``).
THINKING_LEVELS: tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class RemoteModel:
    """One model entry reported by a provider's discovery endpoint."""

    id: str
    name: str | None = None
    contextWindow: int | None = None
    maxTokens: int | None = None


@dataclass(frozen=True)
class FetchResult:
    """The outcome of one discovery request.

    ``error`` being set means the request failed (HTTP error, timeout,
    connection failure, unparseable body). The message is user-facing and must
    never contain a credential.
    """

    url: str
    models: tuple[RemoteModel, ...] = ()
    error: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None


def base_display_name(model: RemoteModel) -> str:
    """Return the display name new model entries default to."""
    name = (model.name or "").strip()
    return name or model.id


# --- non-text model filter -------------------------------------------------

#: Long, distinctive substrings: no realistic text model contains them.
_SUBSTRING_KEYWORDS: tuple[str, ...] = (
    "video",
    "audio",
    "rerank",
    "re-rank",
    "whisper",
    "speech",
    "midjourney",
)

#: Standalone tokens: only matched when neither side is a letter, so digit
#: suffixes such as ``tts1`` still match while ``invoice`` and ``medallion``
#: survive.
_TOKEN_KEYWORDS: tuple[str, ...] = (
    "tts",
    "stt",
    "asr",
    "voice",
    "dall",
    "dalle",
    "sora",
    "imagen",
    "music",
    "image",
    "embed",
    "embedding",
    "embeddings",
    "diffusion",
)

#: Chinese roots for media models; ``name`` containing 理解 exempts them,
#: because 图像理解 / 视频理解 name vision-language chat models.
_CHINESE_KEYWORDS: tuple[str, ...] = (
    "视频",
    "图像",
    "图片",
    "音频",
    "语音",
    "音乐",
)

_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"(?<![a-z]){re.escape(keyword)}(?![a-z])") for keyword in _TOKEN_KEYWORDS
)


def _ascii_hit(text: str) -> bool:
    lowered = _camel_split(text).lower()
    if any(keyword in lowered for keyword in _SUBSTRING_KEYWORDS):
        return True
    return any(pattern.search(lowered) for pattern in _TOKEN_PATTERNS)


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _camel_split(text: str) -> str:
    """Insert a space at camelCase boundaries so ``OmniVoice`` reads as two words.

    Endpoint ids are routinely written ``OmniVoice`` or ``MultiAudio``; without
    the split the standalone-token rules would never see ``voice``/``audio``.
    Lowercase and hyphenated ids are unaffected, which is why names such as
    ``invoice-parser`` and ``imagenet-classifier`` keep their exemption.
    """
    return _CAMEL_BOUNDARY.sub(" ", text)


def _chinese_hit(text: str) -> bool:
    return any(keyword in text for keyword in _CHINESE_KEYWORDS)


def is_non_text(text: str) -> bool:
    """Whether a single string names a non-text model."""
    return _ascii_hit(text) or _chinese_hit(text)


def is_non_text_model(model_id: str, name: str | None = None) -> bool:
    """Whether a remote entry is a non-text model.

    ``id`` and ``name`` are both inspected. The 理解 exemption applies to the
    Chinese roots only: 图像理解 is a vision-language chat model, while a name
    like ``image-embedder`` stays filtered.
    """
    display = name or ""
    if not _ascii_hit(model_id) and not _ascii_hit(display):
        if "理解" in display:
            return False
        return _chinese_hit(model_id) or _chinese_hit(display)
    return True
