"""Entry-point based plugin discovery.

Third-party tools, agents, and providers plug into BERU through
``importlib.metadata`` entry points (the standard Python distribution hook —
see ``importlib.metadata.entry_points``). BERU reads the groups below:

.. list-table::
   :header-rows: 1

   * - Group
     - Contract
     - Used by
   * - ``beru.tools``
     - Zero-argument callable returning a :class:`backend.tools.base.Tool`
     - :func:`backend.tools.registry.get_tool_registry`
   * - ``beru.agents``
     - Zero-argument callable returning a :class:`backend.agents.base.BaseAgent`
     - :func:`backend.agents.registry.get_agent_registry`
   * - ``beru.llm_providers``
     - Callable of ``settings: Settings`` returning an ``LLMProvider``
     - :mod:`backend.engines.llm.registry`
   * - ``beru.embedding_providers``
     - Callable of ``settings: Settings`` returning an ``EmbeddingProvider``
     - :mod:`backend.engines.embeddings.registry`
   * - ``beru.stt_providers``
     - Callable of ``settings: Settings`` returning an ``STTProvider``
     - :mod:`backend.engines.speech`
   * - ``beru.tts_providers``
     - Callable of ``settings: Settings`` returning a ``TTSProvider``
     - :mod:`backend.engines.speech`

Consumer registries keep their hardcoded built-ins and layer discovered
entry points on top, so a source checkout (whose distributions are never
installed) works unchanged. Built-in names always win over a plugin that
reuses them; colliding or unloadable plugins are skipped with a warning.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import metadata
from typing import TypeVar

from backend.core.logging import get_logger

logger = get_logger(__name__)

#: Entry-point group used by each plugin category.
GROUP_TOOLS = "beru.tools"
GROUP_AGENTS = "beru.agents"
GROUP_LLM = "beru.llm_providers"
GROUP_EMBEDDINGS = "beru.embedding_providers"
GROUP_STT = "beru.stt_providers"
GROUP_TTS = "beru.tts_providers"

F = TypeVar("F", bound=Callable)


def load_entry_point_factories(group: str) -> dict[str, Callable]:
    """Return ``{name: callable}`` for every loadable entry point in ``group``.

    Entry points whose distribution cannot be imported (e.g. a version
    incompatibility) are skipped with a warning rather than aborting startup.
    The group is optional: an empty group yields an empty mapping.
    """
    factories: dict[str, Callable] = {}
    try:
        discovered = metadata.entry_points().select(group=group)
    except Exception as exc:  # noqa: BLE001 - entry-point APIs vary; never fail startup
        logger.warning("Could not read entry points for group '%s': %s", group, exc)
        return factories

    for ep in sorted(discovered, key=lambda item: item.name):
        try:
            callable_obj = ep.load()
        except Exception as exc:  # noqa: BLE001 - a broken plugin must not block the app
            logger.warning(
                "Skipping entry point '%s' in group '%s' (import failed): %s",
                ep.name,
                group,
                exc,
            )
            continue
        factories[ep.name] = callable_obj
    return factories


def merge_with_builtins(
    group: str,
    builtins: dict[str, F],
) -> dict[str, Callable]:
    """Return ``builtins`` overlaid with discovered entry-point factories.

    Installed plugins extend BERU with *new* provider names. On a name
    collision the built-in wins (a plugin must not shadow a reserved name),
    and the plugin is skipped with a warning.
    """
    merged: dict[str, Callable] = dict(builtins)
    for name, factory in load_entry_point_factories(group).items():
        if name in merged:
            logger.warning(
                "Ignoring entry point '%s' in group '%s': name collides with a built-in.",
                name,
                group,
            )
            continue
        merged[name] = factory
    return merged