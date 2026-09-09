"""Tool permission policy — controls which tools can be executed and how.

Each tool declares a list of permission tags (e.g. ``["read_clock"]``,
``["write_file"]``). A :class:`PermissionPolicy` maps agent names to the set of
permissions they are allowed to use. Tools whose required permissions are not
fully satisfied by the policy are blocked.

Destructive/sensitive tools declare ``requires_confirmation=True``. When a tool
requires confirmation, the agent returns a ``pending_confirmation`` result
instead of executing immediately, allowing the caller (API layer / UI) to
collect explicit user approval before re-invoking.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PermissionPolicy:
    """Defines allowed permissions per agent.

    ``global_permissions`` applies to all agents. ``agent_permissions`` maps
    specific agent names to additional allowed permissions. A tool is allowed
    if *all* of its required permissions are in the union of global + agent.
    """

    global_permissions: frozenset[str] = field(default_factory=frozenset)
    agent_permissions: dict[str, frozenset[str]] = field(default_factory=dict)

    def allowed_for(self, agent_name: str, required: list[str]) -> bool:
        """Return True if the agent has all the required permissions."""
        if not required:
            return True
        allowed = self.global_permissions | self.agent_permissions.get(
            agent_name, frozenset()
        )
        return all(p in allowed for p in required)

    def missing_for(self, agent_name: str, required: list[str]) -> list[str]:
        """Return the list of permissions the agent is missing."""
        if not required:
            return []
        allowed = self.global_permissions | self.agent_permissions.get(
            agent_name, frozenset()
        )
        return [p for p in required if p not in allowed]


def default_policy() -> PermissionPolicy:
    """Return the default permission policy.

    Default-deny by construction: unknown agents (and the specialists, which
    only need passive reads) get ``read`` + ``read_clock`` and nothing else.
    Only the core agent ("beru_core") is granted the full interactive set —
    ``write``, ``admin``, ``execute``, ``notify`` and the desktop control tags
    (``screenshot``, ``input``, ``clipboard_read``, ``clipboard_write``) — and
    even then the host-interacting tools carry ``requires_confirmation=True``,
    so every use still needs explicit, per-call user approval.
    """
    return PermissionPolicy(
        global_permissions=frozenset({"read_clock", "read"}),
        agent_permissions={
            "beru_core": frozenset(
                {
                    "read_clock",
                    "read",
                    "write",
                    "admin",
                    "execute",
                    "notify",
                    "screenshot",
                    "input",
                    "clipboard_read",
                    "clipboard_write",
                }
            ),
            "igris": frozenset({"read_clock", "read", "write"}),
        },
    )
