"""Per-tool credential scoping (Stage 5.4).

Tools never receive the global single-user API key.  Instead of handing the
whole :class:`Settings` (including ``BERU_API_KEY``) to a tool, the agent
injects a :class:`ScopedCredentials` view containing only the provider keys
the tool declared via ``Tool.required_credentials`` **and** that sit in the
permitted set below.  The owner credential is structurally impossible to
grant: it is not part of :data:`PERMITTED_CREDENTIAL_NAMES`, and the scope's
representation never exposes stored values.

Usage inside a tool::

    class MyTool(Tool):
        required_credentials = ["llm_api_key"]

        async def run(self, **kwargs):
            key = self.credentials.get("llm_api_key")  # may be "" (unconfigured)
            if not key:
                return ToolResult.failure("LLM API key is not configured.")
            ...
"""
from __future__ import annotations

from types import MappingProxyType

from backend.core.config import Settings

#: Credential names a tool may be granted through :func:`scope_for_tool`.
#: Deliberately excludes the owner credential (``api_key`` / ``BERU_API_KEY``):
#: tools get scoped keys, never the global one.
PERMITTED_CREDENTIAL_NAMES = frozenset({"llm_api_key", "embedding_api_key"})


class ScopedCredentials:
    """A read-only, redacted view of the credentials granted to one tool.

    The underlying mapping is a ``MappingProxyType`` (no assignment, no in-place
    mutation) and values are *never* exposed by :meth:`__repr__` or
    :meth:`__str__`, so a logged or traced scope cannot leak key material.
    """

    __slots__ = ("_values",)

    def __init__(self, values: dict[str, str]) -> None:
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ScopedCredentials is read-only")

    def names(self) -> list[str]:
        """Return the granted credential names (sorted)."""
        return sorted(self._values)

    def has(self, name: str) -> bool:
        """True when *name* is present (may map to an empty value)."""
        return name in self._values

    def get(self, name: str) -> str | None:
        """Return the credential value, or ``None`` if not granted."""
        return self._values.get(name)

    def empty(self) -> bool:
        """True when no credentials are granted."""
        return not self._values

    def __bool__(self) -> bool:
        return bool(self._values)

    def __repr__(self) -> str:
        return f"ScopedCredentials(names={self.names()})"

    __str__ = __repr__


def scope_for_tool(
    settings: Settings | None,
    required_credentials: list[str],
) -> ScopedCredentials:
    """Return the scoped credential view for one tool.

    Only keys declared in *required_credentials* **and** present in
    :data:`PERMITTED_CREDENTIAL_NAMES` are granted.  An empty or absent
    ``Settings`` yields an empty scope.  When a key is in the permitted set
    but is unconfigured (empty string), it is omitted from the granted
    names so tools can distinguish "not configured" from "granted".
    """
    if settings is None:
        return ScopedCredentials({})
    granted: dict[str, str] = {}
    for name in required_credentials or ():
        if name not in PERMITTED_CREDENTIAL_NAMES:
            continue
        value = getattr(settings, name, "")
        if value:
            granted[name] = value
    return ScopedCredentials(granted)
