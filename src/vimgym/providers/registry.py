"""Built-in provider registry (intentionally not a public plugin framework)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .base import ProviderAdapter, SourceSpec
from .claude import CLAUDE_ADAPTER
from .codex import CODEX_ADAPTER


BUILTIN_ADAPTERS: dict[str, ProviderAdapter] = {
    CLAUDE_ADAPTER.provider: CLAUDE_ADAPTER,
    CODEX_ADAPTER.provider: CODEX_ADAPTER,
}


def get_adapter(provider: str) -> ProviderAdapter:
    try:
        return BUILTIN_ADAPTERS[provider]
    except KeyError as exc:
        raise ValueError(f"Unsupported provider: {provider}") from exc


def default_sources(environment: Mapping[str, str]) -> list[SourceSpec]:
    sources: list[SourceSpec] = []
    for adapter in BUILTIN_ADAPTERS.values():
        sources.extend(adapter.default_sources(environment))
    return sources


def iter_adapters() -> Iterable[ProviderAdapter]:
    return BUILTIN_ADAPTERS.values()
